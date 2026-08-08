from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

ProgressCallback = Callable[[str], None]


class CorpusImporter(Protocol):
    """Normalize a corpus into ordinary scenario manifests."""

    name: str

    def import_corpus(self, source: Path, destination: Path,
                      filter_text: str | None = None,
                      progress: ProgressCallback | None = None) -> dict: ...
