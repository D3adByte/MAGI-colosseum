from __future__ import annotations

import datetime as dt
import json
import secrets
import shlex
import random
import shutil
from pathlib import Path

from . import CONTRACT_VERSION, __version__
from .catalog import Catalog
from .errors import ValidationError
from .runners import RUNNERS
from .store import EpisodeStore
from .util import atomic_json


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Engine:
    def __init__(self, catalog: Catalog, state_dir: Path):
        self.catalog = catalog
        self.store = EpisodeStore(state_dir)
        self.state_dir = state_dir

    def _runner(self, name: str):
        try:
            return RUNNERS[name]
        except KeyError as exc:
            raise ValidationError(f"runner is declared but not installed: {name}") from exc

    def validate(self, scenario_id: str) -> dict:
        scenario = self.catalog.get(scenario_id)
        result = self._runner(scenario.runner).validate(scenario)
        return {"contract_version": CONTRACT_VERSION, "scenario_id": scenario.id,
                "manifest_hash": scenario.manifest_hash, **result}

    def build(self, scenario_id: str) -> dict:
        scenario = self.catalog.get(scenario_id)
        return {"contract_version": CONTRACT_VERSION, "scenario_id": scenario.id,
                **self._runner(scenario.runner).build(scenario)}

    def start(self, scenario_id: str, seed: int) -> dict:
        scenario = self.catalog.get(scenario_id)
        episode_id = f"ep-{secrets.token_hex(12)}"
        timestamp = now()
        episode = {"contract_version": CONTRACT_VERSION, "episode_id": episode_id,
                   "scenario_id": scenario.id, "scenario_version": scenario.version,
                   "manifest_hash": scenario.manifest_hash, "runner_version": __version__,
                   "seed": seed,
                   "status": "starting", "created_at": timestamp, "updated_at": timestamp,
                   "launch": {}}
        self.store.put(episode)
        episode_dir = self.state_dir / "episodes" / episode_id
        episode_dir.mkdir(parents=True, exist_ok=False)
        try:
            launch = self._runner(scenario.runner).start(scenario, episode, episode_dir)
            episode["launch"] = launch
            episode["status"] = launch["status"]
        except Exception:
            # Up may have partially succeeded before readiness/build failed.
            # Cleanup is best-effort but always attempted and recorded.
            try:
                cleanup = self._runner(scenario.runner).stop(scenario, episode, episode_dir)
            except Exception as cleanup_error:
                cleanup = {"status": "cleanup_failed", "error": str(cleanup_error)}
            episode["cleanup_after_failure"] = cleanup
            episode["status"] = "failed"
            episode["updated_at"] = now()
            self.store.put(episode)
            raise
        episode["updated_at"] = now()
        self.store.put(episode)
        return self.describe(episode_id, "agent")

    def start_random(self, seed: int, category: str | None = None,
                     tag: str | None = None) -> dict:
        candidates = self.catalog.filter(category, tag)
        valid = [scenario for scenario in candidates
                 if self._runner(scenario.runner).validate(scenario).get("status") == "valid"]
        if not valid:
            raise ValidationError("no validated scenarios match the requested filters")
        selected = random.Random(seed).choice(sorted(valid, key=lambda item: item.id))
        result = self.start(selected.id, seed)
        result["selection"] = {"kind": "deterministic-random", "category": category,
                               "tag": tag, "candidate_count": len(valid)}
        return result

    def certify(self, scenario_id: str, seed: int = 0) -> dict:
        """Exercise a clean build/start/readiness/stop/reset lifecycle."""
        scenario = self.catalog.get(scenario_id)
        episode_id = None
        checks = []
        try:
            validation = self.validate(scenario_id)
            valid = validation.get("status") == "valid"
            checks.append({"id": "validate", "status": "passed" if valid else "failed",
                           "detail": validation})
            if not valid:
                raise ValidationError("runner validation failed")
            build = self.build(scenario_id)
            checks.append({"id": "build", "status": "passed", "detail": build})
            launch = self.start(scenario_id, seed)
            episode_id = launch["episode_id"]
            checks.append({"id": "readiness", "status": "passed"})
            stopped = self.stop(episode_id)
            if stopped["status"] != "stopped":
                raise ValidationError(f"stop returned {stopped['status']}")
            checks.append({"id": "stop", "status": "passed"})
            reset = self.reset(episode_id)
            if reset["status"] != "reset":
                raise ValidationError(f"reset returned {reset['status']}")
            checks.append({"id": "reset", "status": "passed"})
            status = "certified"
            error = None
        except Exception as exc:
            status = "failed"
            error = {"type": type(exc).__name__, "message": str(exc)}
            if episode_id:
                try:
                    self.reset(episode_id)
                except Exception:
                    pass
        result = {"contract_version": CONTRACT_VERSION, "scenario_id": scenario_id,
                  "scenario_version": scenario.version, "manifest_hash": scenario.manifest_hash,
                  "runner_version": __version__, "status": status, "checks": checks,
                  "error": error, "certified_at": now()}
        atomic_json(self.state_dir / "certifications" / f"{scenario_id}.json", result)
        return result

    def status(self, episode_id: str) -> dict:
        episode = self.store.get(episode_id)
        scenario = self.catalog.get(episode["scenario_id"])
        runtime = self._runner(scenario.runner).status(scenario, episode,
                                                       self.state_dir / "episodes" / episode_id)
        return {"contract_version": CONTRACT_VERSION, "episode_id": episode_id,
                "scenario_id": scenario.id, **runtime}

    def describe(self, episode_id: str, audience: str) -> dict:
        if audience not in {"public", "agent"}:
            raise ValidationError("audience must be public or agent")
        episode = self.store.get(episode_id)
        scenario = self.catalog.get(episode["scenario_id"])
        result = {"contract_version": CONTRACT_VERSION, "episode_id": episode_id,
                  "scenario_id": scenario.id, "scenario_version": scenario.version,
                  "status": episode["status"], "seed": episode["seed"]}
        result["scenario"] = scenario.public_view() if audience == "public" else scenario.agent_view()
        if audience == "agent":
            result["capabilities"] = episode.get("launch", {}).get("capabilities", [])
            result["constraints"] = scenario.manifest["constraints"]
        return result

    def handoff(self, episode_id: str, endpoint: str | None = None,
                model: str | None = None, approve: bool = False,
                luc1_dir: str = "~/Luciv3", model_timeout: int = 600,
                otel_endpoint: str = "http://127.0.0.1:6006/v1/traces",
                otel_project: str = "luc1-magi") -> dict:
        briefing = self.describe(episode_id, "agent")
        objective = briefing["scenario"].get("objective", "Analyze the supplied challenge")
        argv = [".venv/bin/luc1-magi", "run", "--objective", objective]
        artifacts = []
        targets = []
        target_containers = []
        recommended_capabilities = []
        for capability in briefing.get("capabilities", []):
            if capability.get("local_path"):
                artifacts.append({key: capability[key] for key in
                                  ("artifact_id", "name", "local_path", "sha256", "size")
                                  if key in capability})
                argv.extend(["--artifact", capability["local_path"]])
            if capability.get("host") and capability.get("port"):
                target = {key: capability[key] for key in
                          ("container_id", "host", "port", "protocol", "url", "service", "network")
                          if key in capability}
                targets.append(target)
                argv.extend(["--target", capability.get("url") or
                             f"{capability['host']}:{capability['port']}"])
                container = capability.get("container_id")
                if container and container not in target_containers:
                    target_containers.append(container)
                recommendation = ("web_observation" if capability.get("protocol") in {"http", "https"}
                                  else "network_service_interaction")
                if recommendation not in recommended_capabilities:
                    recommended_capabilities.append(recommendation)
        if approve:
            argv.append("--approve")
        if target_containers:
            argv.append("--kali")
            for container in target_containers:
                argv.extend(["--lab-container", container])
        if endpoint:
            argv.extend(["--endpoint", endpoint])
        if model:
            argv.extend(["--model", model])
        argv.extend(["--model-timeout", str(model_timeout)])
        if otel_endpoint:
            argv.extend(["--otel-endpoint", otel_endpoint])
        if otel_project:
            argv.extend(["--otel-project", otel_project])
        argv.append("-vv")
        completion = briefing["scenario"].get("completion_criteria") or [
            "Identify the vulnerability or solve the supplied challenge",
            "Record reproducible evidence",
            "Explain the security impact or recovered result",
        ]
        return {"contract_version": CONTRACT_VERSION, "challenge_id": briefing["scenario_id"],
                "episode_id": episode_id, "objective": objective, "artifacts": artifacts,
                "targets": targets, "target_containers": target_containers,
                "network": "luc1-magi-lab" if target_containers else None,
                "authorization": {"authorized": True, "scope": "episode-targets-only",
                                  "episode_id": episode_id,
                                  "prohibited_targets": ["localhost", "host", "control-plane",
                                                         "public-internet"]},
                "completion_criteria": completion,
                "capability_recommendations": {"capabilities": recommended_capabilities,
                                               "kali_executor": bool(target_containers)},
                "constraints": briefing["constraints"], "working_directory": luc1_dir,
                "telemetry": {"endpoint": otel_endpoint, "project": otel_project},
                "argv": argv, "invocation": shlex.join(argv),
                "command": f"cd {luc1_dir}\n\n{shlex.join(argv)}"}


    def stop(self, episode_id: str) -> dict:
        episode = self.store.get(episode_id)
        scenario = self.catalog.get(episode["scenario_id"])
        result = self._runner(scenario.runner).stop(scenario, episode,
                                                    self.state_dir / "episodes" / episode_id)
        episode["status"] = result["status"]
        episode["updated_at"] = now()
        self.store.put(episode)
        return {"contract_version": CONTRACT_VERSION, "episode_id": episode_id, **result}

    def reset(self, episode_id: str) -> dict:
        episode = self.store.get(episode_id)
        scenario = self.catalog.get(episode["scenario_id"])
        result = self._runner(scenario.runner).reset(scenario, episode,
                                                     self.state_dir / "episodes" / episode_id)
        episode["status"] = result["status"]
        episode["updated_at"] = now()
        self.store.put(episode)
        return {"contract_version": CONTRACT_VERSION, "episode_id": episode_id, **result}

    def export(self, episode_id: str, destination: Path) -> dict:
        episode = self.store.get(episode_id)
        source = self.state_dir / "episodes" / episode_id
        base = destination.with_suffix("")
        archive = shutil.make_archive(str(base), "zip", source)
        return {"contract_version": CONTRACT_VERSION, "episode_id": episode_id,
                "archive": archive}
