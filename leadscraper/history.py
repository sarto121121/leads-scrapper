"""Remembers which leads were already exported so later runs only return NEW ones."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .models import Lead
from .pipeline import lead_key


def default_path() -> Path:
    env = os.environ.get("LEADSCRAPER_HISTORY")
    return Path(env) if env else Path.home() / ".leadscraper" / "history.json"


def identities(l: Lead) -> dict[str, str]:
    """The things that identify a business: its listing id, phone, email and name/site key."""
    return {"ids": l.place_id or l.map_url, "phones": l.phone, "emails": l.email, "keys": lead_key(l)}


class History:
    FIELDS = ("ids", "phones", "emails", "keys")

    def __init__(self, path: Path | None = None):
        self.path = path
        self.data: dict[str, set[str]] = {f: set() for f in self.FIELDS}
        if path and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                for f in self.FIELDS:
                    self.data[f] = set(raw.get(f, []))
            except (OSError, ValueError):
                # a damaged file must not silently start a fresh history that overwrites it
                backup = path.with_suffix(".broken")
                path.replace(backup)
                print(f"Warning: history file was unreadable; moved to {backup} and starting fresh.")

    @property
    def exported(self) -> int:
        return len(self.data["keys"])

    @property
    def place_ids(self) -> set[str]:
        return self.data["ids"]

    def seen(self, l: Lead) -> bool:
        return any(v and v in self.data[f] for f, v in identities(l).items())

    def add(self, leads: list[Lead]) -> None:
        for l in leads:
            for f, v in identities(l).items():
                if v:
                    self.data[f].add(v)

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {f: sorted(self.data[f]) for f in self.FIELDS}
        payload["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, self.path)   # atomic: never leaves a half-written history
