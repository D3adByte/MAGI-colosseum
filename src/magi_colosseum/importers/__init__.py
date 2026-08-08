"""Corpus adapters normalize upstream layouts into one scenario contract."""

from pathlib import Path

from .vulhub import import_vulhub
from .dojo import import_dojo
from .base import ProgressCallback

IMPORTERS = {"vulhub": import_vulhub, "dojo": import_dojo}


def run_importer(name: str, source: Path, destination: Path,
                 filter_text: str | None = None,
                 progress: ProgressCallback | None = None) -> dict:
    return IMPORTERS[name](source, destination / name, filter_text, progress)
