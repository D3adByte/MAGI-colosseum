from __future__ import annotations

import json
import re
import shutil
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


def import_dojo(source: Path, destination: Path, filter_text: str | None = None,
                progress: Callable[[str], None] | None = None) -> dict:
    """Import an already-forged CTF-Dojo/CTF-Archive tree.

    This does not run CTF-Forge or an LLM. It consumes the generated contract:
    challenge.json, optional docker-compose.yml, and declared player files.
    """
    source = source.resolve()
    if not source.is_dir():
        raise ValidationError(f"forged Dojo source does not exist: {source}")
    imported, failed = [], []
    challenge_files = sorted(source.rglob("challenge.json"))
    if not challenge_files:
        return {"status": "not_forged", "imported": [], "failed": [{
            "source": str(source),
            "error": "no generated challenge.json files found; run CTF-Forge first",
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
