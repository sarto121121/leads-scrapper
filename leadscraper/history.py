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
    # e_* hold the same identities for leads that were delivered WITH an email. With --require-email a lead
    # earlier exported without one is not "done": it can still become a lead once an email is found.
    FIELDS = ("ids", "phones", "emails", "keys", "e_ids", "e_phones", "e_keys")

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

    def skip_ids(self, need_email: bool = False) -> set[str]:
        """Listing ids that need no further work (Maps skips opening these places)."""
        return self.data["e_ids" if need_email else "ids"]

    def seen(self, l: Lead, need_email: bool = False) -> bool:
        ident = identities(l)
        if not need_email:
            return any(v and v in self.data[f] for f, v in ident.items())
        if l.email and l.email in self.data["emails"]:
            return True
        return any(ident[f] and ident[f] in self.data["e_" + f] for f in ("ids", "phones", "keys"))

    def add(self, leads: list[Lead]) -> None:
        for l in leads:
            ident = identities(l)
            for f, v in ident.items():
                if v:
                    self.data[f].add(v)
                    if l.email and f != "emails":
                        self.data["e_" + f].add(v)

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
