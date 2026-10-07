"""Map free-text business types to OpenStreetMap tag filters."""
from __future__ import annotations

import re

# keyword -> list of (osm key, osm value)
CATEGORY_TAGS: dict[str, list[tuple[str, str]]] = {
    "restaurant": [("amenity", "restaurant")],
    "cafe": [("amenity", "cafe")],
    "coffee shop": [("amenity", "cafe")],
    "fast food": [("amenity", "fast_food")],
    "bar": [("amenity", "bar")],
    "pub": [("amenity", "pub")],
    "bakery": [("shop", "bakery")],
    "hotel": [("tourism", "hotel")],
    "guest house": [("tourism", "guest_house")],
    "hostel": [("tourism", "hostel")],
    "motel": [("tourism", "motel")],
    "dentist": [("amenity", "dentist"), ("healthcare", "dentist")],
    "doctor": [("amenity", "doctors"), ("healthcare", "doctor")],
    "clinic": [("amenity", "clinic"), ("healthcare", "clinic")],
    "hospital": [("amenity", "hospital")],
    "pharmacy": [("amenity", "pharmacy")],
    "veterinary": [("amenity", "veterinary")],
    "optician": [("shop", "optician")],
    "gym": [("leisure", "fitness_centre")],
    "fitness": [("leisure", "fitness_centre")],
    "spa": [("leisure", "spa"), ("amenity", "spa")],
    "beauty salon": [("shop", "beauty")],
    "hairdresser": [("shop", "hairdresser")],
    "barber": [("shop", "hairdresser")],
    "school": [("amenity", "school")],
    "university": [("amenity", "university")],
    "college": [("amenity", "college")],
    "kindergarten": [("amenity", "kindergarten")],
    "language school": [("amenity", "language_school")],
    "driving school": [("amenity", "driving_school")],
    "bank": [("amenity", "bank")],
    "atm": [("amenity", "atm")],
    "insurance": [("office", "insurance")],
    "real estate": [("office", "estate_agent")],
    "estate agent": [("office", "estate_agent")],
    "lawyer": [("office", "lawyer")],
    "law firm": [("office", "lawyer")],
    "accountant": [("office", "accountant")],
    "architect": [("office", "architect")],
    "travel agency": [("shop", "travel_agency"), ("office", "travel_agent")],
    "it company": [("office", "it")],
    "software company": [("office", "it")],
    "company": [("office", "company")],
    "ngo": [("office", "ngo")],
    "supermarket": [("shop", "supermarket")],
    "grocery": [("shop", "convenience"), ("shop", "supermarket")],
    "clothing store": [("shop", "clothes")],
    "shoe store": [("shop", "shoes")],
    "jewelry": [("shop", "jewelry")],
    "furniture": [("shop", "furniture")],
    "electronics": [("shop", "electronics")],
    "mobile shop": [("shop", "mobile_phone")],
    "computer store": [("shop", "computer")],
    "hardware store": [("shop", "hardware"), ("shop", "doityourself")],
    "bookstore": [("shop", "books")],
    "florist": [("shop", "florist")],
    "car dealer": [("shop", "car")],
    "car repair": [("shop", "car_repair")],
    "car wash": [("amenity", "car_wash")],
    "car rental": [("amenity", "car_rental")],
    "fuel": [("amenity", "fuel")],
    "gas station": [("amenity", "fuel")],
    "bike shop": [("shop", "bicycle")],
    "photographer": [("craft", "photographer"), ("shop", "photo")],
    "printing": [("shop", "copyshop"), ("craft", "printer")],
    "laundry": [("shop", "laundry"), ("shop", "dry_cleaning")],
    "electrician": [("craft", "electrician")],
    "plumber": [("craft", "plumber")],
    "carpenter": [("craft", "carpenter")],
    "builder": [("craft", "builder")],
    "construction": [("office", "construction_company"), ("craft", "builder")],
    "wedding": [("shop", "wedding"), ("amenity", "events_venue")],
    "event venue": [("amenity", "events_venue")],
    "coworking": [("amenity", "coworking_space")],
    "cinema": [("amenity", "cinema")],
    "nightclub": [("amenity", "nightclub")],
    "pet shop": [("shop", "pet")],
    "supplier": [("shop", "wholesale")],
    "wholesale": [("shop", "wholesale")],
    "factory": [("man_made", "works"), ("industrial", "factory")],
    "warehouse": [("building", "warehouse")],
    "marketing agency": [("office", "advertising_agency")],
    "advertising agency": [("office", "advertising_agency")],
    "recruitment": [("office", "employment_agency")],
    "telecom": [("office", "telecommunication"), ("shop", "mobile_phone")],
}

_ALIASES = {"restaurants": "restaurant", "dentists": "dentist", "hotels": "hotel",
            "doctors": "doctor", "clinics": "clinic", "gyms": "gym", "schools": "school",
            "lawyers": "lawyer", "cafes": "cafe", "salon": "beauty salon", "salons": "beauty salon",
            "pharmacies": "pharmacy", "banks": "bank", "companies": "company",
            "real estate agent": "real estate", "realtor": "real estate",
            "software house": "software company", "it": "it company"}


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def resolve(category: str) -> tuple[list[str], bool]:
    """Return (overpass selector fragments, exact_match).

    Accepts a known keyword, a raw ``key=value`` OSM tag, or any free text
    (falls back to a case-insensitive name search, exact_match=False).
    """
    c = re.sub(r"\s+", " ", category.strip().lower())
    c = _ALIASES.get(c, c)
    if c in CATEGORY_TAGS:
        return [f'["{k}"="{_esc(v)}"]' for k, v in CATEGORY_TAGS[c]], True
    m = re.fullmatch(r"([a-z_:]+)=([a-z0-9_:\-]+)", c)
    if m:
        return [f'["{m.group(1)}"="{m.group(2)}"]'], True
    # singular fallback: "dentists" -> "dentist"
    if c.endswith("s") and c[:-1] in CATEGORY_TAGS:
        return resolve(c[:-1])
    return [f'["name"~"{_esc(re.escape(c))}",i]'], False
