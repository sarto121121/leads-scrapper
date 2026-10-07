"""Free worldwide business data from OpenStreetMap (Nominatim + Overpass). No API key needed."""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from .categories import resolve
from .models import Lead

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
UA = "LeadScraper/1.0 (business lead research tool)"


@dataclass
class Place:
    display_name: str
    country_code: str
    country: str
    osm_type: str
    osm_id: int
    bbox: tuple[float, float, float, float]  # south, west, north, east


def geocode(city: str, country: str, session: requests.Session | None = None) -> Place:
    s = session or requests.Session()
    r = s.get(NOMINATIM, params={"q": f"{city}, {country}", "format": "jsonv2", "limit": 5,
                                  "addressdetails": 1}, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    results = r.json()
    if not results:
        raise ValueError(f"Could not find '{city}, {country}'. Check the spelling.")
    # prefer an administrative/place relation (gives an exact area, not a rectangle)
    best = next((x for x in results if x["osm_type"] == "relation"
                 and x.get("category") in ("boundary", "place")), results[0])
    south, north, west, east = (float(v) for v in best["boundingbox"])
    addr = best.get("address", {})
    time.sleep(1.0)  # Nominatim usage policy: max 1 request/second
    return Place(best["display_name"], addr.get("country_code", "").upper(), addr.get("country", country),
                 best["osm_type"], int(best["osm_id"]), (south, west, north, east))


def build_query(place: Place, category: str, timeout: int = 180) -> tuple[str, bool]:
    selectors, exact = resolve(category)
    if place.osm_type == "relation":
        head, scope = f"area(id:{3600000000 + place.osm_id})->.a;", "(area.a)"
    elif place.osm_type == "way":
        head, scope = f"area(id:{2400000000 + place.osm_id})->.a;", "(area.a)"
    else:
        s, w, n, e = place.bbox
        head, scope = "", f"({s},{w},{n},{e})"
    body = "\n".join(f"  nwr{sel}{scope};" for sel in selectors)
    return f"[out:json][timeout:{timeout}];\n{head}\n(\n{body}\n);\nout tags center;", exact


def run_overpass(query: str, session: requests.Session | None = None, retries: int = 2) -> list[dict]:
    s = session or requests.Session()
    last: Exception | None = None
    for attempt in range(retries + 1):
        for url in OVERPASS_MIRRORS:
            try:
                r = s.post(url, data={"data": query}, headers={"User-Agent": UA}, timeout=240)
                if r.status_code in (429, 502, 503, 504):
                    last = RuntimeError(f"{url} -> HTTP {r.status_code}")
                    continue
                r.raise_for_status()
                return r.json().get("elements", [])
            except (requests.RequestException, ValueError) as e:
                last = e
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"All Overpass servers failed: {last}")


def _address(t: dict) -> str:
    if t.get("addr:full"):
        return t["addr:full"]
    street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
    parts = [t.get("addr:unit"), street, t.get("addr:suburb") or t.get("addr:neighbourhood"),
             t.get("addr:city") or t.get("addr:town") or t.get("addr:village"),
             t.get("addr:state"), t.get("addr:postcode")]
    return ", ".join(p for p in parts if p)


def parse_elements(elements: list[dict], category: str, city: str, country: str) -> list[Lead]:
    leads = []
    for el in elements:
        t = el.get("tags", {})
        name = t.get("name") or t.get("name:en") or t.get("brand") or t.get("operator")
        if not name:
            continue
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        lead = Lead(
            name=name.strip(), category=category, address=_address(t), city=city, country=country,
            website=(t.get("website") or t.get("contact:website") or t.get("url") or "").strip(),
            lat=lat, lon=lon, source="OpenStreetMap",
            map_url=f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
        )
        for k in ("phone", "contact:phone", "mobile", "contact:mobile"):
            if t.get(k):
                lead.raw_phones.append(t[k])
        for k in ("email", "contact:email"):
            if t.get(k):
                lead.raw_emails += [e for e in t[k].replace(",", ";").split(";") if e.strip()]
        for k, net in (("contact:facebook", "facebook"), ("contact:instagram", "instagram")):
            if t.get(k):
                lead.socials[net] = t[k]
        leads.append(lead)
    return leads


def search(city: str, country: str, category: str, log=print) -> tuple[list[Lead], Place]:
    s = requests.Session()
    log(f"Locating {city}, {country} ...")
    place = geocode(city, country, s)
    log(f"  -> {place.display_name}")
    query, exact = build_query(place, category)
    if not exact:
        log(f"  '{category}' is not a known type; falling back to a name search "
            f"(try a type like 'restaurant' or a tag like 'shop=bakery' for better results)")
    log("Querying OpenStreetMap (can take up to a minute for big cities) ...")
    leads = parse_elements(run_overpass(query, s), category, city, country)
    log(f"  -> {len(leads)} named places found")
    return leads, place
