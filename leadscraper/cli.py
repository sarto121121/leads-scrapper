from __future__ import annotations

import argparse
import sys
from datetime import datetime

from pathlib import Path

from . import maps, osm, places
from .history import History, default_path
from .export import safe_filename, write_xlsx
from .models import Lead
from .pipeline import clean_website, enrich_all, finalize, lead_key, merge
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
    p.add_argument("-n", "--count", "--limit", dest="count", type=int, default=0,
                   help="EXACT number of leads wanted per business type. Keeps searching until it has that "
                        "many complete leads (0 = take everything found)")
    p.add_argument("--website", choices=["any", "yes", "no"], default="any",
                   help="yes = only businesses that have a website; no = only those WITHOUT one "
                        "(a Facebook/Instagram page does not count as a website)")
    p.add_argument("--include-seen", action="store_true",
                   help="Also return leads that earlier runs already exported (default: only NEW leads)")
    p.add_argument("--reset-history", action="store_true", help="Forget all previously exported leads")
    p.add_argument("--history", help=f"History file (default: {default_path()})")
    p.add_argument("--keep-incomplete", action="store_true",
                   help="Also keep leads that have neither a phone nor an email (dropped by default)")
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


class Collector:
    """Receives raw leads in batches, enriches them and keeps the ones that qualify."""

    def __init__(self, a: argparse.Namespace, region: str | None, hist: History, run: History):
        self.a, self.region = a, region
        self.hist, self.run = hist, run     # hist: earlier runs (on disk); run: this run so far
        self.leads: list[Lead] = []
        self.seen: set[str] = set()
        self.skipped = 0

    def _known(self, l: Lead) -> bool:
        return self.run.seen(l) or (not self.a.include_seen and self.hist.seen(l))

    def qualifies(self, l: Lead) -> bool:
        a = self.a
        if not a.keep_incomplete and not (l.phone or l.email):
            return False
        if a.require_email and not l.email:
            return False
        if a.require_phone and not l.phone:
            return False
        has_site = bool(l.website)
        return not ((a.website == "yes" and not has_site) or (a.website == "no" and has_site))

    @property
    def done(self) -> bool:
        return bool(self.a.count) and len(self.leads) >= self.a.count

    def feed(self, batch: list[Lead]) -> bool:
        """Process a batch; return True when the wanted number of leads has been reached."""
        fresh = []
        for l in batch:
            clean_website(l)
        for l in merge(batch):
            k = lead_key(l)
            if k in self.seen:
                continue
            self.seen.add(k)
            if self._known(l):          # same listing/site exported before: no need to crawl it again
                self.skipped += 1
                continue
            fresh.append(l)
        if fresh:
            if self.a.no_website_crawl:
                finalize(fresh, self.region, not self.a.no_dns_check)
            else:
                enrich_all(fresh, self.region, self.a.workers, self.a.timeout, not self.a.no_dns_check, _log)
            for l in fresh:
                if not self.qualifies(l):
                    continue
                if self._known(l):      # same phone/email as a lead we already have or exported
                    self.skipped += 1
                    continue
                self.leads.append(l)
                self.run.add([l])
            target = f"/{self.a.count}" if self.a.count else ""
            _log(f"  qualified leads so far: {len(self.leads)}{target}")
        return self.done


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
    region = a.region or country_region(a.country)
    if not region and not use_osm:
        _log(f"Warning: could not work out the phone country code for '{a.country}'. Local-format numbers "
             f"will be dropped; add --region XX (two-letter code, e.g. --region GB).")

    hist_path = Path(a.history) if a.history else default_path()
    if a.reset_history and hist_path.exists():
        hist_path.unlink()
        _log(f"History cleared ({hist_path}).")
    hist = History(hist_path)
    run = History(None)
    if hist.exported and not a.include_seen:
        _log(f"History: {hist.exported} leads exported before will be skipped (use --include-seen to keep them).")

    result: list[Lead] = []
    skipped_total = 0
    for cat in categories:
        _log(f"\n=== {cat} in {a.city}, {a.country}"
             + (f" (target: exactly {a.count} leads) ===" if a.count else " ==="))
        col = Collector(a, region, hist, run)
        if use_maps:
            try:
                maps.search(a.city, a.country, cat, col.feed, areas, more_queries=bool(a.count),
                            headless=not a.show_browser, log=_log,
                            skip_ids=frozenset() if a.include_seen else frozenset(hist.place_ids))
            except maps.MapsError as e:
                _log(f"Google Maps: {e}")
                if not (use_api or use_osm) and not col.leads:
                    return 1
        others: list[Lead] = []
        if use_osm and not col.done:
            try:
                got, place = osm.search(a.city, a.country, cat, _log)
                col.region = col.region or place.country_code
                others += got
            except (ValueError, RuntimeError) as e:
                _log(f"OpenStreetMap: {e}")
        if use_api and not col.done:
            others += places.search(a.city, a.country, cat, log=_log)
        others.sort(key=lambda l: -(bool(l.website) + bool(l.raw_phones) + bool(l.raw_emails)))
        for i in range(0, len(others), 20):
            if col.done or col.feed(others[i:i + 20]):
                break
        leads = col.leads[:a.count] if a.count else col.leads
        if a.count and len(leads) < a.count:
            _log(f"Only {len(leads)} of the {a.count} requested '{cat}' leads exist for these filters. "
                 f"To get more: add --areas \"Area1,Area2,...\", pick a larger city, or relax "
                 f"--require-email/--require-phone/--website.")
        if col.skipped:
            _log(f"  skipped {col.skipped} leads already exported before or duplicated in this run")
        skipped_total += col.skipped
        run.add(leads)
        result += leads

    if not result:
        if skipped_total:
            _log(f"No NEW leads: all {skipped_total} matches were already exported in earlier runs. Try "
                 f"--areas for other neighbourhoods, a different city/type, or --include-seen.")
        else:
            _log("No leads found. Try a broader business type or a larger city.")
        return 1

    result.sort(key=lambda l: -(bool(l.email) + bool(l.phone)))
    hist.add(result)
    out = a.output or f"leads_{safe_filename(a.category, a.city, a.country)}.xlsx"
    meta = {"Country": a.country, "City": a.city, "Categories": ", ".join(categories),
            "Sources": ", ".join(sorted({x for l in result for x in l.source.split(' + ')}))}
    try:
        write_xlsx(result, out, meta)
    except PermissionError:   # the file is open in Excel
        out = out.removesuffix(".xlsx") + f"_{datetime.now():%H%M%S}.xlsx"
        _log(f"Could not overwrite the file (is it open in Excel?). Saving as {out} instead.")
        write_xlsx(result, out, meta)
    hist.save()
    _log(f"\nDone: {len(result)} leads "
         f"({sum(bool(l.email) for l in result)} with email, {sum(bool(l.phone) for l in result)} with phone, "
         f"{sum(bool(l.website) for l in result)} with website)\nSaved to {out}\n"
         f"History: {hist.exported} leads remembered in {hist_path} (next run returns only new ones)")
    return 0
