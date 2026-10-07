"""Optional Google Places (New) source. Enabled when GOOGLE_MAPS_API_KEY is set.

Google has far more complete phone/website data than OpenStreetMap in many countries.
Each text search returns at most 60 results, so we also search with sub-queries.
"""
from __future__ import annotations

import os

import requests

from .models import Lead

URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ("places.displayName,places.formattedAddress,places.internationalPhoneNumber,"
          "places.nationalPhoneNumber,places.websiteUri,places.googleMapsUri,places.location,"
          "places.businessStatus,nextPageToken")


def api_key() -> str:
    return os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()


def search(city: str, country: str, category: str, max_results: int = 60, log=print) -> list[Lead]:
    key = api_key()
    if not key:
        return []
    s = requests.Session()
    leads: list[Lead] = []
    queries = [f"{category} in {city}, {country}"]
    seen: set[str] = set()
    for q in queries:
        token = None
        while len(leads) < max_results:
            body = {"textQuery": q, "pageSize": 20}
            if token:
                body["pageToken"] = token
            r = s.post(URL, json=body, timeout=30,
                       headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS})
            if r.status_code != 200:
                log(f"  Google Places error {r.status_code}: {r.text[:200]}")
                break
            data = r.json()
            for p in data.get("places", []):
                if p.get("businessStatus") == "CLOSED_PERMANENTLY":
                    continue
                uri = p.get("googleMapsUri", "")
                if uri in seen:
                    continue
                seen.add(uri)
                loc = p.get("location", {})
                lead = Lead(name=p.get("displayName", {}).get("text", ""), category=category,
                            address=p.get("formattedAddress", ""), city=city, country=country,
                            website=p.get("websiteUri", ""), lat=loc.get("latitude"),
                            lon=loc.get("longitude"), source="Google Places", map_url=uri)
                for k in ("internationalPhoneNumber", "nationalPhoneNumber"):
                    if p.get(k):
                        lead.raw_phones.append(p[k])
                if lead.name:
                    leads.append(lead)
            token = data.get("nextPageToken")
            if not token:
                break
    log(f"  -> {len(leads)} places from Google Places")
    return leads
