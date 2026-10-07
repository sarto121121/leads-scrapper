from __future__ import annotations

import argparse
import sys

from . import osm, places
from .export import safe_filename, write_xlsx
from .models import Lead
from .pipeline import enrich_all, finalize, merge


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="leadscraper",
        description="Generate business leads (name, phone, email, address, website) for a city into Excel.")
    p.add_argument("-c", "--country", help="Country, e.g. 'Pakistan'")
    p.add_argument("-t", "--city", help="City, e.g. 'Lahore'")
    p.add_argument("-k", "--category", help="Business type(s), comma separated: 'dentist,gym' "
                   "(or an OSM tag such as shop=bakery)")
    p.add_argument("-o", "--output", help="Output .xlsx path (default: auto-named)")
    p.add_argument("--source", choices=["auto", "osm", "google"], default="auto",
                   help="auto = OpenStreetMap, plus Google Places if GOOGLE_MAPS_API_KEY is set")
    p.add_argument("--limit", type=int, default=0, help="Max leads per category (0 = no limit)")
    p.add_argument("--no-website-crawl", action="store_true", help="Skip visiting websites (faster, fewer emails)")
    p.add_argument("--no-dns-check", action="store_true", help="Skip email domain verification")
    p.add_argument("--require-email", action="store_true", help="Keep only leads that have a verified email")
    p.add_argument("--require-phone", action="store_true", help="Keep only leads that have a valid phone")
    p.add_argument("--workers", type=int, default=24, help="Parallel website fetches (default 24)")
    p.add_argument("--timeout", type=float, default=8.0, help="Per-request timeout seconds for websites")
    a = p.parse_args(argv)
    for attr, prompt in (("country", "Country"), ("city", "City"), ("category", "Business type(s)")):
        if not getattr(a, attr):
            if not sys.stdin.isatty():
                p.error(f"--{attr} is required")
            setattr(a, attr, input(f"{prompt}: ").strip())
        if not getattr(a, attr):
            p.error(f"{attr} cannot be empty")
    return a


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    categories = [c.strip() for c in a.category.split(",") if c.strip()]
    use_osm = a.source in ("auto", "osm")
    use_google = a.source == "google" or (a.source == "auto" and bool(places.api_key()))
    if a.source == "google" and not places.api_key():
        _log("GOOGLE_MAPS_API_KEY is not set.")
        return 2

    all_leads: list[Lead] = []
    region = None
    for cat in categories:
        _log(f"\n=== {cat} in {a.city}, {a.country} ===")
        found: list[Lead] = []
        if use_osm:
            try:
                got, place = osm.search(a.city, a.country, cat, _log)
                region = region or place.country_code
                found += got
            except (ValueError, RuntimeError) as e:
                _log(f"OpenStreetMap: {e}")
                if not use_google:
                    return 1
        if use_google:
            found += places.search(a.city, a.country, cat, log=_log)
        found = merge(found)
        if a.limit:
            # prefer leads that already carry contact data when trimming
            found.sort(key=lambda l: -(bool(l.website) + bool(l.raw_phones) + bool(l.raw_emails)))
            found = found[:a.limit]
        all_leads += found

    all_leads = merge(all_leads)
    if not all_leads:
        _log("No leads found. Try a broader business type or a larger city.")
        return 1

    if a.no_website_crawl:
        finalize(all_leads, region, not a.no_dns_check)
    else:
        enrich_all(all_leads, region, a.workers, a.timeout, not a.no_dns_check, _log)

    if a.require_email:
        all_leads = [l for l in all_leads if l.email]
    if a.require_phone:
        all_leads = [l for l in all_leads if l.phone]
    all_leads.sort(key=lambda l: (-(bool(l.email) + bool(l.phone)), l.name.lower()))

    out = a.output or f"leads_{safe_filename(a.category, a.city, a.country)}.xlsx"
    write_xlsx(all_leads, out, {"Country": a.country, "City": a.city, "Categories": ", ".join(categories),
                                "Sources": ", ".join(sorted({s for l in all_leads for s in l.source.split(' + ')}))})
    _log(f"\nDone: {len(all_leads)} leads "
         f"({sum(bool(l.email) for l in all_leads)} with email, {sum(bool(l.phone) for l in all_leads)} with phone, "
         f"{sum(l.has_website for l in all_leads)} with website)\nSaved to {out}")
    return 0
