from __future__ import annotations

import argparse
import sys

from . import maps, osm, places
from .export import safe_filename, write_xlsx
from .models import Lead
from .pipeline import enrich_all, finalize, merge
from .validate import country_region


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
    p.add_argument("--source", choices=["maps", "api", "osm", "all"], default="maps",
                   help="maps = Google Maps via browser (default, no key); api = official Google Places "
                        "API (needs GOOGLE_MAPS_API_KEY); osm = OpenStreetMap; all = everything")
    p.add_argument("--areas", help="Comma-separated neighbourhoods to search one by one for MORE results, "
                   "e.g. 'DHA,Gulberg,Johar Town' (Google shows ~120 results per search)")
    p.add_argument("--show-browser", action="store_true", help="Show the browser window (to solve a captcha)")
    p.add_argument("--region", help="Two-letter country code for phone parsing (auto-detected normally)")
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
    use_maps = a.source in ("maps", "all")
    use_api = a.source in ("api", "all")
    use_osm = a.source in ("osm", "all")
    if use_api and not places.api_key():
        _log("GOOGLE_MAPS_API_KEY is not set (needed for --source api).")
        return 2
    areas = [x.strip() for x in (a.areas or "").split(",") if x.strip()]

    all_leads: list[Lead] = []
    region = a.region or country_region(a.country)
    for cat in categories:
        _log(f"\n=== {cat} in {a.city}, {a.country} ===")
        found: list[Lead] = []
        if use_maps:
            try:
                found += maps.search(a.city, a.country, cat, areas, a.limit, not a.show_browser, log=_log)
            except maps.MapsError as e:
                _log(f"Google Maps: {e}")
                if not (use_api or use_osm):
                    return 1
        if use_osm:
            try:
                got, place = osm.search(a.city, a.country, cat, _log)
                region = region or place.country_code
                found += got
            except (ValueError, RuntimeError) as e:
                _log(f"OpenStreetMap: {e}")
        if use_api:
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
