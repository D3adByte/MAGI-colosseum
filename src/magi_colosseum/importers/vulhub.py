from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from ..errors import ValidationError
from ..model import validate_manifest
from ..util import atomic_json, sha256_file

COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
WEB_PORTS = {80, 443, 3000, 5000, 7001, 8000, 8080, 8081, 8088, 8888, 9000, 9200}
LAB_NETWORK = "luc1-magi-lab"


def _slug(relative: Path) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(relative).lower()).strip("-")
    return f"vulhub-{value}"[:128]


def _compose_file(directory: Path) -> Path | None:
    return next((directory / name for name in COMPOSE_NAMES if (directory / name).is_file()), None)


def _canonical_compose(compose_file: Path) -> dict:
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "config", "--format", "json"],
        cwd=compose_file.parent, capture_output=True, text=True,
    )
    if result.returncode:
        raise ValidationError(result.stderr.strip() or f"cannot normalize {compose_file}")
    data = json.loads(result.stdout)
    data.pop("name", None)
    return data


def _services(data: dict, snapshot_root: Path) -> list[dict]:
    candidates = []
    for name, service in data.get("services", {}).items():
        ports = service.get("ports", [])
        for port in ports:
            target = int(port["target"] if isinstance(port, dict) else str(port).rsplit(":", 1)[-1].split("/")[0])
            is_web = target in WEB_PORTS
            candidates.append({"name": name, "capability": "web_endpoint" if is_web else "interactive_service",
                               "protocol": "http" if is_web else "tcp", "internal_port": target})
        # Preserve build/runtime semantics while preventing original fixed or
        # wildcard host publication. Luc1 reaches registered container DNS
        # identities on the external internal lab network instead.
        service.pop("ports", None)
        service.pop("network_mode", None)
        service["networks"] = ["default"]
        for volume in service.get("volumes", []):
            if isinstance(volume, dict) and volume.get("type") == "bind":
                source = Path(volume["source"]).resolve()
                if source != snapshot_root and snapshot_root not in source.parents:
                    raise ValidationError(f"bind mount escapes imported snapshot: {source}")
                volume["read_only"] = True
    unique = {(item["name"], item["internal_port"]): item for item in candidates}
    data["networks"] = {"default": {"external": True, "name": LAB_NETWORK}}
    return list(unique.values())


def import_vulhub(source: Path, destination: Path, filter_text: str | None = None,
                  progress: Callable[[str], None] | None = None) -> dict:
    source = source.resolve()
    if not source.is_dir():
        raise ValidationError(f"Vulhub source does not exist: {source}")
    imported, failed, unsupported = [], [], []
    directories = sorted({path.parent for name in COMPOSE_NAMES for path in source.rglob(name)})
    directories = [directory for directory in directories
                   if not filter_text or filter_text.lower() in
                   str(directory.relative_to(source)).lower()]
    if progress:
        progress(f"vulhub: discovered {len(directories)} matching labs")
    for position, directory in enumerate(directories, 1):
        relative = directory.relative_to(source)
        if progress:
            progress(f"vulhub: [{position}/{len(directories)}] normalizing {relative}")
        try:
            scenario_id = _slug(relative)
            output = destination / scenario_id
            output.mkdir(parents=True, exist_ok=True)
            snapshot = output / "content"
            if snapshot.exists():
                shutil.rmtree(snapshot)
            if any(path.is_symlink() for path in directory.rglob("*")):
                raise ValidationError("symlinks are not accepted in imported scenario snapshots")
            shutil.copytree(directory, snapshot)
            compose_file = _compose_file(snapshot)
            compose = _canonical_compose(compose_file)
            services = _services(compose, snapshot.resolve())
            if not services:
                raise ValidationError("no published challenge service found")
            normalized = output / "compose.normalized.json"
            atomic_json(normalized, compose)
            primary_web = any(item["capability"] == "web_endpoint" for item in services)
            manifest = {
                "manifest_version": "1.0", "scenario_version": "1.0.0", "id": scenario_id,
                "title": f"Imported security service {scenario_id.removeprefix('vulhub-')}",
                "category": "web" if primary_web else "network-service", "difficulty": "unspecified",
                "tags": ["imported", "vulhub", "lifecycle-only"],
                "source": {"kind": "vulhub", "uri": str(relative), "license": "upstream",
                           "provenance": "Vulhub local checkout"},
                "platform": {"os": ["linux"], "architectures": ["host"]},
                "objective": "Assess the authorized service and document the security impact.",
                "completion_criteria": ["Identify the vulnerability",
                                        "Record reproducible evidence",
                                        "Explain the security impact"],
                "capabilities": [{"kind": "web_endpoint" if primary_web else "network_target"}],
                "artifacts": [], "build": {"required": True, "network_access": "image-pull-only"},
                "runtime": {"runner": "compose", "compose_file": "compose.normalized.json", "services": services},
                "constraints": {"network_access": "luc1-magi-lab-only", "filesystem": "none",
                                "wall_time_seconds": 1800, "cpu_count": 2, "memory_bytes": 2147483648,
                                "process_limit": 512, "disk_bytes": 10737418240},
                "readiness": {"kind": "tcp", "service": services[0]["name"], "timeout_seconds": 120},
                "cleanup": {"strategy": "compose-down-volumes", "idempotent": True},
                "timeout": {"wall_time_seconds": 1800, "on_timeout": "stop"},
                "determinism": {"supported": False, "seed_scope": "none"},
            }
            validate_manifest(manifest, output)
            atomic_json(output / "scenario.json", manifest)
            imported.append({"scenario_id": scenario_id, "source": str(relative)})
        except Exception as exc:
            (output / "scenario.json").unlink(missing_ok=True)
            message = str(exc)
            if message == "no published challenge service found":
                unsupported.append({"source": str(relative), "reason": "no_agent_endpoint",
                                    "detail": "lab requires an interactive container shell"})
            elif "env file" in message and "not found" in message:
                unsupported.append({"source": str(relative), "reason": "external_configuration",
                                    "detail": message})
            elif "forbidden host capability" in message:
                unsupported.append({"source": str(relative), "reason": "forbidden_host_capability",
                                    "detail": message})
            else:
                failed.append({"source": str(relative), "error": message})
            if progress:
                state = "unsupported" if len(unsupported) and unsupported[-1]["source"] == str(relative) \
                    else "failed"
                progress(f"vulhub: [{position}/{len(directories)}] {state} {relative}: {exc}")
    if progress:
        progress(f"vulhub: complete ({len(imported)} imported, {len(unsupported)} unsupported, "
                 f"{len(failed)} failed)")
    return {"status": "complete" if not failed and not unsupported else "partial",
            "imported": imported, "failed": failed, "unsupported": unsupported,
            "imported_count": len(imported), "failed_count": len(failed),
            "unsupported_count": len(unsupported)}
