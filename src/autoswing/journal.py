"""Append-only JSONL journal. Every broker interaction is recorded here.

One file per UTC day: journal/2026-07-07.jsonl
Entries are never modified or deleted.
"""

import json
from datetime import datetime, timezone
from pathlib import Path


class Journal:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path_for_today(self) -> Path:
        return self.dir / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"

    def record(self, event: str, **payload) -> dict:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            **payload,
        }
        with open(self._path_for_today(), "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return entry
