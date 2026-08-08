from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .util import canonical_json, safe_join, sha256_bytes, sha256_file

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
ALLOWED_CAPABILITIES = {
    "network_target", "web_endpoint", "interactive_service", "provided_file",
    "source_tree", "binary", "memory_image", "disk_image", "packet_capture",
    "credentials", "multi_service",
}
ALLOWED_RUNNERS = {"file", "compose"}


@dataclass(frozen=True)
class Scenario:
    root: Path
    manifest: dict[str, Any]
    manifest_hash: str

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def version(self) -> str:
        return self.manifest["scenario_version"]

    @property
    def runner(self) -> str:
        return self.manifest["runtime"]["runner"]

    def public_view(self) -> dict[str, Any]:
        allowed = ("manifest_version", "scenario_version", "id", "title", "category",
                   "difficulty", "tags", "source", "platform", "capabilities")
        return {key: self.manifest[key] for key in allowed if key in self.manifest}

    def agent_view(self) -> dict[str, Any]:
        # Provenance is intentionally not inherited: a corpus path, CVE URL, or
        # product name can solve a black-box identification task by itself.
        result = {key: value for key, value in self.public_view().items() if key != "source"}
        for key in ("objective", "constraints", "completion_criteria"):
            if key in self.manifest:
                result[key] = self.manifest[key]
        return result


def load_scenario(root: Path) -> Scenario:
    manifest_path = root / "scenario.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot load {manifest_path}: {exc}") from exc
    content_root = Path(manifest.get("_content_root", root)).resolve()
    if not content_root.is_dir():
        raise ValidationError("scenario content root does not exist")
    validate_manifest(manifest, content_root)
    return Scenario(content_root, manifest, sha256_bytes(canonical_json(manifest)))


def validate_manifest(data: dict[str, Any], root: Path) -> None:
    required = {"manifest_version", "scenario_version", "id", "title", "category",
                "difficulty", "tags", "source", "platform", "capabilities", "artifacts",
                "build", "runtime", "constraints", "readiness", "cleanup", "timeout",
                "determinism"}
    missing = sorted(required - data.keys())
    if missing:
        raise ValidationError(f"manifest missing fields: {', '.join(missing)}")
    if data["manifest_version"] != "1.0":
        raise ValidationError("unsupported manifest_version")
    if not ID_RE.fullmatch(data["id"]):
        raise ValidationError("scenario id is not stable-ID safe")
    if data["runtime"].get("runner") not in ALLOWED_RUNNERS:
        raise ValidationError("unknown runner")
    kinds = {item.get("kind") for item in data["capabilities"]}
    unknown = kinds - ALLOWED_CAPABILITIES
    if unknown:
        raise ValidationError(f"unknown capabilities: {sorted(unknown)}")
    for artifact in data["artifacts"]:
        try:
            path = safe_join(root, artifact["source"])
        except (KeyError, ValueError) as exc:
            raise ValidationError(str(exc)) from exc
        if not path.is_file():
            raise ValidationError(f"artifact does not exist: {artifact.get('source')}")
        if sha256_file(path) != artifact.get("sha256"):
            raise ValidationError(f"artifact hash mismatch: {artifact.get('id')}")
    forbidden = {"flag", "answer", "expected_answer", "evaluator", "secret", "token",
                 "success", "partial_credit", "expected_artifacts"}
    leaked = forbidden.intersection(data)
    if leaked:
        raise ValidationError(f"unsupported scoring or secret keys in lifecycle manifest: {sorted(leaked)}")
    runtime = data["runtime"]
    for build_input in data["build"].get("inputs", []):
        try:
            build_path = safe_join(root, build_input["path"])
        except (KeyError, ValueError) as exc:
            raise ValidationError(str(exc)) from exc
        if not build_path.is_file() or sha256_file(build_path) != build_input.get("sha256"):
            raise ValidationError(f"build input hash mismatch: {build_input.get('path')}")
    if runtime["runner"] == "compose":
        compose = safe_join(root, runtime.get("compose_file", ""))
        if not compose.is_file():
            raise ValidationError("compose_file does not exist")
        text = compose.read_text(errors="replace")
        dangerous = ("privileged: true", "/var/run/docker.sock", "network_mode: host", "pid: host")
        if any(marker in text for marker in dangerous):
            raise ValidationError("compose definition requests a forbidden host capability")
        if compose.suffix != ".json":
            raise ValidationError("compose definitions must be normalized JSON")
        if compose.suffix == ".json":
            definition = json.loads(text)
            networks = definition.get("networks", {})
            if networks != {"default": {"external": True, "name": "luc1-magi-lab"}}:
                raise ValidationError("compose must use only external network luc1-magi-lab")
            for service in definition.get("services", {}).values():
                if service.get("privileged") or service.get("devices") or service.get("pid") == "host":
                    raise ValidationError("compose definition requests a forbidden host capability")
                if service.get("networks") != ["default"]:
                    raise ValidationError("every compose service must use only luc1-magi-lab")
                if service.get("ports") or service.get("network_mode"):
                    raise ValidationError("compose services may not publish host ports or set network_mode")
                for volume in service.get("volumes", []):
                    if isinstance(volume, dict) and volume.get("type") == "bind":
                        try:
                            source = Path(volume["source"]).resolve()
                            source.relative_to(root.resolve())
                        except (KeyError, ValueError) as exc:
                            raise ValidationError("compose bind mount escapes scenario snapshot") from exc
                        if not volume.get("read_only"):
                            raise ValidationError("compose bind mounts must be read-only")
