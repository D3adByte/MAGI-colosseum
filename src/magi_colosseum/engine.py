from __future__ import annotations

import datetime as dt
import secrets
import random
from pathlib import Path
from collections.abc import Callable

from . import CONTRACT_VERSION, __version__
from .catalog import Catalog
from .errors import ValidationError
from .runners import RUNNERS
from .store import EpisodeStore


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Engine:
    def __init__(self, catalog: Catalog, state_dir: Path, workspace_root: Path | None = None):
        self.catalog = catalog
        self.store = EpisodeStore(state_dir)
        self.state_dir = state_dir
        self.workspace_root = workspace_root or state_dir / "episodes"

    def _episode_dir(self, episode: dict) -> Path:
        # workspace was added after the first contract version; retain the
        # fallback so existing local episode records can still be cleaned up.
        return Path(episode.get("workspace", self.state_dir / "episodes" /
                                episode["episode_id"]))

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
        episode_dir = self.workspace_root / episode_id
        episode["workspace"] = str(episode_dir)
        self.store.put(episode)
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
                     tag: str | None = None,
                     progress: Callable[[str], None] | None = None) -> dict:
        candidates = self.catalog.filter(category, tag)
        valid = []
        for position, scenario in enumerate(candidates, 1):
            if progress:
                progress(f"validating [{position}/{len(candidates)}] {scenario.id}")
            if self._runner(scenario.runner).validate(scenario).get("status") == "valid":
                valid.append(scenario)
        if not valid:
            raise ValidationError("no validated scenarios match the requested filters")
        selected = random.Random(seed).choice(sorted(valid, key=lambda item: item.id))
        if progress:
            progress(f"selected {selected.id}; starting {selected.runner} lab")
        result = self.start(selected.id, seed)
        if progress:
            progress(f"ready {result['episode_id']}")
        result["selection"] = {"kind": "deterministic-random", "category": category,
                               "tag": tag, "candidate_count": len(valid)}
        return result

    def status(self, episode_id: str) -> dict:
        episode = self.store.get(episode_id)
        scenario = self.catalog.get(episode["scenario_id"])
        runtime = self._runner(scenario.runner).status(scenario, episode,
                                                       self._episode_dir(episode))
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
            capabilities = episode.get("launch", {}).get("capabilities", [])
            result["artifacts"] = [item for item in capabilities if item.get("local_path")]
            result["targets"] = [item for item in capabilities
                                 if item.get("host") and item.get("port")]
            result["prompt"] = self._target_prompt(result["artifacts"], result["targets"])
            result["constraints"] = scenario.manifest["constraints"]
        return result

    @staticmethod
    def _target_prompt(artifacts: list[dict], targets: list[dict]) -> str:
        lines = []
        for target in targets:
            address = target.get("url") or f"{target['host']}:{target['port']}"
            label = "Web service" if target.get("protocol") in {"http", "https"} \
                else "Network service"
            lines.append(f"{label} is running at {address}")
        for artifact in artifacts:
            lines.append(f"Challenge file is located at {artifact['local_path']}")
        return "\n".join(lines)


    def stop(self, episode_id: str) -> dict:
        episode = self.store.get(episode_id)
        scenario = self.catalog.get(episode["scenario_id"])
        result = self._runner(scenario.runner).stop(scenario, episode,
                                                    self._episode_dir(episode))
        episode["status"] = result["status"]
        episode["updated_at"] = now()
        self.store.put(episode)
        return {"contract_version": CONTRACT_VERSION, "episode_id": episode_id, **result}

    def reset(self, episode_id: str) -> dict:
        episode = self.store.get(episode_id)
        scenario = self.catalog.get(episode["scenario_id"])
        result = self._runner(scenario.runner).reset(scenario, episode,
                                                     self._episode_dir(episode))
        if result["status"] == "ready":
            episode["launch"] = result
        episode["status"] = result["status"]
        episode["updated_at"] = now()
        self.store.put(episode)
        response = self.describe(episode_id, "agent")
        response["reset"] = result["status"] == "ready"
        return response
