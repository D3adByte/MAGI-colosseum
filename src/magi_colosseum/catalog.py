from __future__ import annotations

from pathlib import Path

from .errors import NotFoundError, ValidationError
from .model import Scenario, load_scenario


class Catalog:
    def __init__(self, roots: list[Path]):
        self.roots = roots
        self._cache: list[Scenario] | None = None

    def refresh(self) -> None:
        self._cache = None

    def paths(self) -> list[Path]:
        found: set[Path] = set()
        for root in self.roots:
            if root.is_dir():
                found.update(path.parent for path in root.rglob("scenario.json"))
        return sorted(found)

    def all(self) -> list[Scenario]:
        if self._cache is not None:
            return list(self._cache)
        scenarios: list[Scenario] = []
        ids: set[str] = set()
        for path in self.paths():
            try:
                scenario = load_scenario(path)
            except ValidationError:
                # An import created by an older contract must not make every
                # unrelated catalog command unusable. Re-importing that corpus
                # replaces it with the current normalized manifest.
                continue
            if scenario.id in ids:
                raise ValidationError(f"duplicate scenario id: {scenario.id}")
            ids.add(scenario.id)
            scenarios.append(scenario)
        self._cache = sorted(scenarios, key=lambda item: item.id)
        return list(self._cache)

    def get(self, scenario_id: str) -> Scenario:
        for scenario in self.all():
            if scenario.id == scenario_id:
                return scenario
        raise NotFoundError(f"scenario not found: {scenario_id}")

    def filter(self, category: str | None = None, tag: str | None = None) -> list[Scenario]:
        return [scenario for scenario in self.all()
                if (not category or scenario.manifest.get("category") == category)
                and (not tag or tag in scenario.manifest.get("tags", []))]
