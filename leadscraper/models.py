from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Lead:
    name: str
    category: str = ""
    address: str = ""
    city: str = ""
    country: str = ""
    phone: str = ""              # E.164, e.g. +923001234567
    phone_display: str = ""      # international format
    phone_type: str = ""         # Mobile / Landline / ...
    email: str = ""              # best email
    other_emails: list[str] = field(default_factory=list)
    email_status: str = ""
    email_note: str = ""         # why there is no email (shown in the Email column)
    site_state: str = ""         # ok / down / blocked / "" (website not visited)
    website: str = ""
    website_status: str = ""     # Yes / No
    socials: dict[str, str] = field(default_factory=dict)
    lat: float | None = None
    lon: float | None = None
    source: str = ""
    map_url: str = ""
    place_id: str = ""           # stable listing id (used to avoid repeats across runs)
    # raw values collected before validation (not exported)
    raw_phones: list[str] = field(default_factory=list)
    raw_emails: list[str] = field(default_factory=list)

    @property
    def has_website(self) -> bool:
        return bool(self.website)
