from __future__ import annotations

import json
import re
import shutil
from html import unescape
from pathlib import Path
from typing import Callable

from ..errors import ValidationError
from ..model import validate_manifest
from ..util import atomic_json, safe_join, sha256_file
from .vulhub import _canonical_compose, _compose_file, _services

CATEGORY_MAP = {
    "rev": "reverse", "reversing": "reverse", "reverse engineering": "reverse",
    "pwn": "binary-exploitation", "crypto": "cryptography", "forensics": "forensics",
    "web": "web", "misc": "miscellaneous",
}

RAW_CATEGORY_MAP = {
    "REV": "reverse-engineering", "REVERSE": "reverse-engineering",
    "PWN": "binary-exploitation", "CRYPTO": "cryptography",
    "FORENSICS": "forensics", "FORENSIC": "forensics", "WEB": "web",
    "MISC": "miscellaneous", "OSINT": "osint", "BLOCKCHAIN": "blockchain",
}

ADMIN_NAMES = {
    "description.md", "rehost.md", "challenge.json", "module.yml", ".flag.sha256",
    ".init", "flag", "flag.txt", "flagcheck", "dockerfile",
    "docker-compose.yml", "docker-compose.yaml",
    "compose.yml", "compose.yaml",
}
ADMIN_MARKERS = ("solve", "solution", "writeup", "flagcheck")


def _slug(relative: Path) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(relative).lower()).strip("-")
    return f"dojo-{value}"[:128]


def _artifact_kind(path: Path) -> tuple[str, list[dict]]:
    suffix = path.suffix.lower()
    if suffix in {".pcap", ".pcapng"}:
        return "packet_capture", [{"kind": "provided_file"}, {"kind": "packet_capture"}]
    if suffix == ".apk":
        return "provided_file", [{"kind": "provided_file"}]
    if suffix in {".img", ".raw", ".vmdk", ".vdi"}:
        return "disk_image", [{"kind": "provided_file"}, {"kind": "disk_image"}]
    return "provided_file", [{"kind": "provided_file"}]


def _raw_entries(module: Path) -> list[dict]:
    entries = []
    current = None
    for line in module.read_text(errors="replace").splitlines():
        match = re.match(r"\s*-\s+id:\s*(.+?)\s*$", line)
        if match:
            current = {"id": match.group(1).strip().strip("'\"")}
            entries.append(current)
            continue
        match = re.match(r"\s+name:\s*(.+?)\s*$", line)
        if match and current is not None and "name" not in current:
            current["name"] = unescape(match.group(1).strip().strip("'\""))
    return [entry for entry in entries if entry.get("id")]


def _raw_category(name: str) -> tuple[str, str, str]:
    name = unescape(name)
    parts = [part.strip() for part in name.split(" - ", 2)]
    raw = parts[0].upper() if parts else "MISC"
    category = RAW_CATEGORY_MAP.get(raw, "miscellaneous")
    points = parts[1] if len(parts) == 3 and parts[1].isdigit() else "unspecified"
    title = parts[2] if len(parts) == 3 else name
    return category, points, title


def _raw_player_files(task_root: Path) -> list[Path]:
    result = []
    for path in sorted(task_root.rglob("*")):
        if path.is_symlink():
            raise ValidationError("symlinks are not accepted in raw archive challenges")
        if not path.is_file():
            continue
        relative = path.relative_to(task_root)
        lowered = relative.name.lower()
        if lowered in ADMIN_NAMES or lowered.startswith(".flag"):
            continue
        if any(marker in lowered for marker in ADMIN_MARKERS):
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        result.append(path)
    return result


def _import_raw_archive(source: Path, destination: Path, filter_text: str | None,
                        progress: Callable[[str], None] | None) -> dict:
    discovered = []
    for module in sorted(source.glob("*/module.yml")):
        for entry in _raw_entries(module):
            task_root = module.parent / entry["id"]
            if task_root.is_dir():
                relative = task_root.relative_to(source)
                if not filter_text or filter_text.lower() in str(relative).lower():
                    discovered.append((relative, task_root, entry))
    if progress:
        progress(f"dojo: discovered {len(discovered)} raw archive challenges")
    imported, failed, skipped = [], [], []
    for position, (relative, task_root, entry) in enumerate(discovered, 1):
        output = destination / _slug(relative)
        try:
            category, points, title = _raw_category(entry.get("name", entry["id"]))
            # Raw web/pwn challenges normally require a forge-generated service.
            # Never pretend their server-side files are contestant artifacts.
            if category in {"web", "binary-exploitation"}:
                skipped.append({"source": str(relative), "reason": "service_requires_forge"})
                continue
            files = _raw_player_files(task_root)
            if not files:
                skipped.append({"source": str(relative), "reason": "no_safe_player_files"})
                continue
            output.mkdir(parents=True, exist_ok=True)
            artifacts, capabilities, seen = [], [], set()
            for index, path in enumerate(files, 1):
                kind, file_capabilities = _artifact_kind(path)
                suffix = "".join(path.suffixes)
                artifacts.append({"id": f"artifact-{index}", "kind": kind,
                                  "source": str(path.relative_to(task_root)),
                                  "name": f"artifact-{index}{suffix}",
                                  "sha256": sha256_file(path)})
                for capability in file_capabilities:
                    if capability["kind"] not in seen:
                        capabilities.append(capability)
                        seen.add(capability["kind"])
            manifest = {
                "manifest_version": "1.0", "scenario_version": "1.0.0",
                "id": _slug(relative), "title": title, "category": category,
                # Archive points reflect an event's scoring curve, not a
                # portable challenge difficulty rating.
                "difficulty": "unspecified",
                "tags": ["imported", "ctf-dojo", "raw-archive"],
                "source": {"kind": "ctf-dojo", "uri": str(relative),
                           "license": "verify-upstream", "provenance": "pwn.college CTF Archive",
                           "upstream_points": int(points) if points != "unspecified" else None},
                "platform": {"os": ["any"], "architectures": ["any"]},
                "objective": "Analyze the supplied challenge files and recover the requested value.",
                "capabilities": capabilities, "artifacts": artifacts,
                "completion_criteria": ["Solve the supplied challenge"],
                "build": {"required": False, "network_access": False},
                "runtime": {"runner": "file"},
                "constraints": {"network_access": False, "filesystem": "episode-only",
                                "wall_time_seconds": 1800, "cpu_count": 2,
                                "memory_bytes": 2147483648, "process_limit": 512,
                                "disk_bytes": 10737418240},
                "readiness": {"kind": "none"},
                "cleanup": {"strategy": "delete-episode-workspace", "idempotent": True},
                "timeout": {"wall_time_seconds": 1800, "on_timeout": "stop"},
                "determinism": {"supported": False, "seed_scope": "none"},
                "_content_root": str(task_root.resolve()),
            }
            validate_manifest(manifest, task_root)
            atomic_json(output / "scenario.json", manifest)
            imported.append({"scenario_id": manifest["id"], "source": str(relative),
                             "category": category})
        except Exception as exc:
            (output / "scenario.json").unlink(missing_ok=True)
            failed.append({"source": str(relative), "error": str(exc)})
        if progress and (position == 1 or position % 50 == 0 or position == len(discovered)):
            progress(f"dojo: [{position}/{len(discovered)}] "
                     f"{len(imported)} imported, {len(skipped)} skipped, {len(failed)} failed")
    return {"status": "complete" if not failed else "partial", "format": "raw-archive",
            "imported": imported, "failed": failed, "skipped": skipped,
            "imported_count": len(imported), "failed_count": len(failed),
            "skipped_count": len(skipped)}


def import_dojo(source: Path, destination: Path, filter_text: str | None = None,
                progress: Callable[[str], None] | None = None) -> dict:
    """Import a raw CTF Archive or an already-forged CTF-Dojo tree.

    Forged trees use challenge.json contracts and may include Compose services.
    Raw archives use module.yml metadata and conservatively import safe offline
    player files while reporting service challenges that still require forging.
    """
    source = source.resolve()
    if not source.is_dir():
        raise ValidationError(f"forged Dojo source does not exist: {source}")
    imported, failed = [], []
    challenge_files = sorted(source.rglob("challenge.json"))
    if not challenge_files:
        module_files = list(source.glob("*/module.yml"))
        if module_files:
            return _import_raw_archive(source, destination, filter_text, progress)
        return {"status": "not_forged", "imported": [], "failed": [{
            "source": str(source), "error": "no challenge.json or module.yml files found",
        }], "imported_count": 0, "failed_count": 1}
    challenge_files = [path for path in challenge_files
                       if not filter_text or filter_text.lower() in
                       str(path.parent.relative_to(source)).lower()]
    if progress:
        progress(f"dojo: discovered {len(challenge_files)} matching forged challenges")
    for position, challenge_file in enumerate(challenge_files, 1):
        task_root = challenge_file.parent
        relative = task_root.relative_to(source)
        output = None
        if progress:
            progress(f"dojo: [{position}/{len(challenge_files)}] normalizing {relative}")
        try:
            upstream = json.loads(challenge_file.read_text())
            scenario_id = _slug(relative)
            output = destination / scenario_id
            output.mkdir(parents=True, exist_ok=True)
            snapshot = output / "content"
            if snapshot.exists():
                shutil.rmtree(snapshot)
            if any(path.is_symlink() for path in task_root.rglob("*")):
                raise ValidationError("symlinks are not accepted in imported snapshots")
            shutil.copytree(task_root, snapshot)

            artifacts, capabilities = [], []
            seen_capabilities = set()
            declared_files = upstream.get("files") or []
            if not isinstance(declared_files, list) or not all(
                    isinstance(item, str) for item in declared_files):
                raise ValidationError("challenge files must be a list of relative paths")
            for index, relative_file in enumerate(declared_files):
                path = safe_join(snapshot, relative_file)
                if not path.is_file():
                    raise ValidationError(f"declared player file is missing: {relative_file}")
                kind, file_capabilities = _artifact_kind(path)
                artifacts.append({"id": f"artifact-{index + 1}", "kind": kind,
                                  "source": str(path.relative_to(output)),
                                  "name": str(Path(relative_file)),
                                  "sha256": sha256_file(path)})
                for capability in file_capabilities:
                    if capability["kind"] not in seen_capabilities:
                        capabilities.append(capability)
                        seen_capabilities.add(capability["kind"])

            compose_source = _compose_file(snapshot)
            wants_service = bool(upstream.get("compose"))
            services = []
            runtime = {"runner": "file"}
            readiness = {"kind": "none"}
            if wants_service:
                if compose_source is None:
                    raise ValidationError("challenge declares compose=true but has no Compose file")
                compose = _canonical_compose(compose_source)
                services = _services(compose, snapshot.resolve())
                if not services:
                    raise ValidationError("Compose challenge exposes no player service")
                normalized = output / "compose.normalized.json"
                atomic_json(normalized, compose)
                runtime = {"runner": "compose", "compose_file": normalized.name, "services": services}
                readiness = {"kind": "tcp", "service": services[0]["name"], "timeout_seconds": 120}
                for service in services:
                    kind = service["capability"]
                    if kind not in seen_capabilities:
                        capabilities.append({"kind": kind})
                        seen_capabilities.add(kind)

            description = str(upstream.get("description") or "Analyze the supplied challenge.").strip()
            hidden_flag = upstream.get("flag")
            if hidden_flag and hidden_flag in description:
                description = description.replace(hidden_flag, "[redacted]")
            category_raw = str(upstream.get("category") or "misc").lower()
            category = CATEGORY_MAP.get(category_raw, category_raw)
            manifest = {
                "manifest_version": "1.0", "scenario_version": "1.0.0", "id": scenario_id,
                "title": str(upstream.get("name") or relative.name), "category": category,
                "difficulty": "unspecified", "tags": ["imported", "ctf-dojo", "lifecycle-only"],
                "source": {"kind": "ctf-dojo", "uri": str(relative),
                           "license": "verify-upstream", "provenance": "forged CTF-Dojo tree"},
                "platform": {"os": ["linux" if wants_service else "any"], "architectures": ["host"]},
                "objective": description, "capabilities": capabilities, "artifacts": artifacts,
                "completion_criteria": ["Solve the supplied challenge",
                                        "Record reproducible evidence",
                                        "Explain the recovered result or security impact"],
                "build": {"required": wants_service,
                          "network_access": "image-pull-only" if wants_service else False},
                "runtime": runtime,
                "constraints": {"network_access": "luc1-magi-lab-only" if wants_service else False,
                                "filesystem": "episode-only", "wall_time_seconds": 1800,
                                "cpu_count": 2, "memory_bytes": 2147483648,
                                "process_limit": 512, "disk_bytes": 10737418240},
                "readiness": readiness,
                "cleanup": {"strategy": "compose-down-volumes" if wants_service
                             else "delete-episode-workspace", "idempotent": True},
                "timeout": {"wall_time_seconds": 1800, "on_timeout": "stop"},
                "determinism": {"supported": False, "seed_scope": "none"},
            }
            validate_manifest(manifest, output)
            atomic_json(output / "scenario.json", manifest)
            imported.append({"scenario_id": scenario_id, "source": str(relative)})
        except Exception as exc:
            if output is not None:
                (output / "scenario.json").unlink(missing_ok=True)
            failed.append({"source": str(relative), "error": str(exc)})
            if progress:
                progress(f"dojo: [{position}/{len(challenge_files)}] failed {relative}: {exc}")
    if progress:
        progress(f"dojo: complete ({len(imported)} imported, {len(failed)} failed)")
    return {"status": "complete" if not failed else "partial", "imported": imported,
            "failed": failed, "imported_count": len(imported), "failed_count": len(failed)}
