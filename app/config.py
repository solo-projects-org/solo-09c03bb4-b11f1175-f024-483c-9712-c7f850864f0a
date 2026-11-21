"""Runtime configuration, all overridable through environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    archive_dir: Path
    manifest_name: str = "manifest.json"
    max_detail_rows: int = 5000

    @classmethod
    def from_env(cls) -> Settings:
        archive_dir = Path(os.environ.get("ARCHIVE_DIR", "/archive"))
        manifest_name = os.environ.get("ARCHIVE_MANIFEST", "manifest.json").strip()
        if not manifest_name:
            raise ValueError("ARCHIVE_MANIFEST must not be empty")
        raw_rows = os.environ.get("MAX_DETAIL_ROWS", "5000")
        try:
            max_rows = int(raw_rows)
        except ValueError as exc:
            raise ValueError("MAX_DETAIL_ROWS must be an integer") from exc
        if max_rows <= 0:
            raise ValueError("MAX_DETAIL_ROWS must be positive")
        return cls(
            archive_dir=archive_dir,
            manifest_name=manifest_name,
            max_detail_rows=max_rows,
        )
