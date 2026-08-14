from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Protocol

from .errors import BuildError, ReadinessTimeout, StartError
from .model import Scenario
from .util import safe_join, sha256_file

LAB_NETWORK = "luc1-magi-lab"


class ScenarioRunner(Protocol):
    def validate(self, scenario: Scenario) -> dict: ...
    def build(self, scenario: Scenario) -> dict: ...
    def start(self, scenario: Scenario, episode: dict, episode_dir: Path) -> dict: ...
    def status(self, scenario: Scenario, episode: dict, episode_dir: Path) -> dict: ...
    def stop(self, scenario: Scenario, episode: dict, episode_dir: Path) -> dict: ...
    def reset(self, scenario: Scenario, episode: dict, episode_dir: Path) -> dict: ...


def _stage_artifacts(scenario: Scenario, episode_dir: Path) -> list[dict]:
    delivery = episode_dir / "artifacts"
    delivery.mkdir(parents=True, exist_ok=True)
    descriptors = []
    for item in scenario.manifest["artifacts"]:
        source = safe_join(scenario.root, item["source"])
        target = safe_join(delivery, item["name"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o444)
        descriptors.append({"kind": item["kind"], "artifact_id": item["id"],
                            "name": item["name"], "sha256": sha256_file(target),
                            "size": target.stat().st_size, "local_path": str(target)})
    return descriptors


class FileRunner:
    def validate(self, scenario: Scenario) -> dict:
        return {"status": "valid", "runner": "file"}

    def build(self, scenario: Scenario) -> dict:
        return {"status": "built", "runner": "file", "reproducible": True}

    def start(self, scenario: Scenario, episode: dict, episode_dir: Path) -> dict:
        return {"status": "ready", "capabilities": _stage_artifacts(scenario, episode_dir),
                "endpoints": []}

    def status(self, scenario: Scenario, episode: dict, episode_dir: Path) -> dict:
        return {"status": episode["status"]}

    def stop(self, scenario: Scenario, episode: dict, episode_dir: Path) -> dict:
        shutil.rmtree(episode_dir, ignore_errors=True)
        return {"status": "stopped"}

    def reset(self, scenario: Scenario, episode: dict, episode_dir: Path) -> dict:
        self.stop(scenario, episode, episode_dir)
        episode_dir.mkdir(parents=True, exist_ok=False)
        return self.start(scenario, episode, episode_dir)


class ComposeRunner(FileRunner):
    def _compose(self, scenario: Scenario, episode: dict, *args: str) -> list[str]:
        compose_file = safe_join(scenario.root, scenario.manifest["runtime"]["compose_file"])
        command = ["docker", "compose", "-p", f"magi_{episode['episode_id'].replace('-', '')}",
                   "-f", str(compose_file)]
        override = episode.get("runner_state", {}).get("override_file")
        if override:
            command.extend(["-f", override])
        return [*command, *args]

    def validate(self, scenario: Scenario) -> dict:
        if shutil.which("docker") is None:
            return {"status": "invalid", "runner": "compose", "errors": ["docker unavailable"]}
        network_error = self._network_error()
        if network_error:
            return {"status": "invalid", "runner": "compose", "errors": [network_error]}
        result = subprocess.run(self._compose(scenario, {"episode_id": "validation"}, "config", "--quiet"),
                                capture_output=True, text=True)
        return {"status": "valid" if result.returncode == 0 else "invalid", "runner": "compose",
                "errors": [] if result.returncode == 0 else [result.stderr.strip()]}

    def build(self, scenario: Scenario) -> dict:
        episode = {"episode_id": "build"}
        result = subprocess.run(self._compose(scenario, episode, "build", "--pull=false"),
                                capture_output=True, text=True)
        if result.returncode:
            raise BuildError(result.stderr.strip() or "compose build failed")
        return {"status": "built", "runner": "compose"}

    def start(self, scenario: Scenario, episode: dict, episode_dir: Path) -> dict:
        network_error = self._network_error()
        if network_error:
            raise StartError(network_error)
        artifacts = _stage_artifacts(scenario, episode_dir)
        override = episode_dir / "compose.episode.json"
        compose_services = self._service_names(scenario, episode)
        services = {
            name: {"container_name": self._container_name(episode["episode_id"], position),
                   "networks": ["default"]}
            for position, name in enumerate(compose_services, 1)
        }
        override.write_text(json.dumps({"services": services}, sort_keys=True))
        episode["runner_state"] = {"override_file": str(override),
                                   "container_names": {name: item["container_name"]
                                                       for name, item in services.items()},
                                   "network": LAB_NETWORK}
        result = subprocess.run(self._compose(scenario, episode, "up", "-d", "--force-recreate"),
                                capture_output=True, text=True)
        (episode_dir / "infrastructure-start.log").write_text(result.stdout + result.stderr)
        if result.returncode:
            raise StartError(result.stderr.strip() or "compose start failed")
        self._verify_attachments(episode)
        endpoints = self._registered_endpoints(scenario, episode)
        self._wait_ready(scenario, episode, endpoints)
        return {"status": "ready", "capabilities": artifacts + endpoints,
                "endpoints": endpoints, "network": LAB_NETWORK,
                "target_containers": sorted({item["container_id"] for item in endpoints})}

    def _registered_endpoints(self, scenario: Scenario, episode: dict) -> list[dict]:
        result = []
        for service in scenario.manifest["runtime"].get("services", []):
            container = episode["runner_state"]["container_names"][service["name"]]
            result.append({"kind": service["capability"], "service": service["name"],
                           "container_id": container, "host": container,
                           "protocol": service.get("protocol", "tcp"),
                           "port": int(service["internal_port"]), "network": LAB_NETWORK})
            if service["capability"] == "web_endpoint":
                result[-1]["url"] = (f"{service.get('protocol', 'http')}://{container}:"
                                     f"{int(service['internal_port'])}/")
        return result

    def _wait_ready(self, scenario: Scenario, episode: dict, endpoints: list[dict]) -> None:
        check = scenario.manifest["readiness"]
        if check.get("kind") == "none":
            return
        deadline = time.monotonic() + check.get("timeout_seconds", 30)
        endpoint = next((item for item in endpoints if item.get("service") == check.get("service")), None)
        host = self._container_ip(episode, endpoint["service"]) if endpoint else None
        while endpoint and time.monotonic() < deadline:
            try:
                with socket.create_connection((host, endpoint["port"]), timeout=1):
                    return
            except OSError:
                time.sleep(0.2)
        raise ReadinessTimeout("service did not become ready")

    def _network_error(self) -> str | None:
        proc = subprocess.run(["docker", "network", "inspect", LAB_NETWORK,
                               "--format", "{{json .Internal}}"],
                              capture_output=True, text=True)
        if proc.returncode:
            return (f"required external Docker network {LAB_NETWORK} is missing; "
                    "run Luc1-MAGI ./setup.sh")
        if proc.stdout.strip() != "true":
            return f"Docker network {LAB_NETWORK} must be internal"
        return None

    def _service_names(self, scenario: Scenario, episode: dict) -> list[str]:
        proc = subprocess.run(self._compose(scenario, episode, "config", "--services"),
                              capture_output=True, text=True)
        if proc.returncode:
            raise StartError(proc.stderr.strip() or "cannot enumerate Compose services")
        names = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if not names:
            raise StartError("Compose scenario contains no services")
        return names

    @staticmethod
    def _container_name(episode_id: str, position: int) -> str:
        # Runtime names are contestant-visible DNS. Never put scenario IDs,
        # product names, CVEs, or upstream service names in them.
        suffix = re.sub(r"[^a-f0-9]", "", episode_id.lower().removeprefix("ep-"))[:16]
        return f"colosseum-{suffix}-target-{position}"

    def _verify_attachments(self, episode: dict) -> None:
        for service, container in episode["runner_state"]["container_names"].items():
            proc = subprocess.run(["docker", "inspect", container, "--format",
                                   "{{json .NetworkSettings.Networks}}"],
                                  capture_output=True, text=True)
            if proc.returncode:
                raise StartError(f"cannot inspect registered container {container}")
            networks = set(json.loads(proc.stdout or "{}").keys())
            if networks != {LAB_NETWORK}:
                raise StartError(f"container {container} attached to forbidden networks: "
                                 f"{sorted(networks - {LAB_NETWORK})}")

    def _container_ip(self, episode: dict, service: str) -> str:
        container = episode["runner_state"]["container_names"][service]
        proc = subprocess.run(["docker", "inspect", container, "--format",
                               f'{{{{with index .NetworkSettings.Networks "{LAB_NETWORK}"}}}}'
                               "{{.IPAddress}}{{end}}"], capture_output=True, text=True)
        address = proc.stdout.strip()
        if proc.returncode or not address:
            raise StartError(f"container {container} is not attached to {LAB_NETWORK}")
        return address

    def status(self, scenario: Scenario, episode: dict, episode_dir: Path) -> dict:
        proc = subprocess.run(self._compose(scenario, episode, "ps", "--format", "json"),
                              capture_output=True, text=True)
        return {"status": episode["status"] if proc.returncode == 0 else "failed",
                "infrastructure": proc.stdout.strip()}

    def stop(self, scenario: Scenario, episode: dict, episode_dir: Path) -> dict:
        logs = subprocess.run(self._compose(scenario, episode, "logs", "--no-color"),
                              capture_output=True, text=True)
        episode_dir.mkdir(parents=True, exist_ok=True)
        (episode_dir / "infrastructure.log").write_text(logs.stdout + logs.stderr)
        proc = subprocess.run(self._compose(scenario, episode, "down", "--volumes", "--remove-orphans"),
                              capture_output=True, text=True)
        if proc.returncode:
            return {"status": "cleanup_failed"}
        shutil.rmtree(episode_dir, ignore_errors=True)
        return {"status": "stopped"}

    def reset(self, scenario: Scenario, episode: dict, episode_dir: Path) -> dict:
        result = self.stop(scenario, episode, episode_dir)
        if result["status"] == "cleanup_failed":
            return result
        episode_dir.mkdir(parents=True, exist_ok=False)
        return self.start(scenario, episode, episode_dir)


RUNNERS: dict[str, ScenarioRunner] = {"file": FileRunner(), "compose": ComposeRunner()}
