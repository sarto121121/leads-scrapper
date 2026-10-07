"""Orchestration: collect -> dedupe -> enrich from websites -> validate."""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from .enrich import Robots, crawl, is_social, make_session
from .models import Lead
from .validate import best_phone, verify_emails


def lead_key(l: Lead) -> str:
    host = (urlparse(l.website if "://" in l.website else "//" + l.website).hostname or "").removeprefix("www.")
    name = re.sub(r"[^\w]+", "", l.name.lower())
    return f"{host}|{name}" if host else f"{name}|{re.sub(r'[^0-9]', '', l.raw_phones[0])[-8:] if l.raw_phones else l.address.lower()}"


def merge(leads: list[Lead]) -> list[Lead]:
    """Drop duplicates, filling gaps in the first-seen record from later ones."""
    out: dict[str, Lead] = {}
    by_name: dict[str, Lead] = {}
    for l in leads:
        nm = re.sub(r"[^\w]+", "", l.name.lower())
        cur = out.get(lead_key(l)) or by_name.get(nm if _close(by_name.get(nm), l) else "")
        if cur is None:
            out[lead_key(l)] = l
            by_name.setdefault(nm, l)
            continue
        cur.address = cur.address or l.address
        cur.website = cur.website or l.website
        cur.raw_phones += [p for p in l.raw_phones if p not in cur.raw_phones]
        cur.raw_emails += [e for e in l.raw_emails if e not in cur.raw_emails]
        for k, v in l.socials.items():
            cur.socials.setdefault(k, v)
        if l.source not in cur.source:
            cur.source += f" + {l.source}"
    return list(out.values())


def _close(a: Lead | None, b: Lead) -> bool:
    """Same name counts as the same business only if their coordinates are within ~300 m (or unknown)."""
    if a is None:
        return False
    if None in (a.lat, a.lon, b.lat, b.lon):
        return False
    return abs(a.lat - b.lat) < 0.003 and abs(a.lon - b.lon) < 0.003


def enrich_all(leads: list[Lead], region: str | None, workers: int = 24, timeout: float = 8.0,
               check_dns: bool = True, log=print) -> None:
    session = make_session()
    robots = Robots(session, timeout)
    todo = [l for l in leads if l.website]
    done = [0]
    lock = threading.Lock()

    def work(l: Lead) -> None:
        if is_social(l.website):
            l.website_status = "Social page only"   # nothing to crawl on a Facebook/Instagram page
            return
        info = crawl(l.website, region, session, robots, timeout)
        l.website_status = "Yes" if info.reachable else "Yes (not loading)"
        l.raw_emails += [e for e in info.emails if e not in l.raw_emails]
        # phones from the website are only a fallback, listed after directory data
        l.raw_phones += [p for p in info.phones[:3] if p not in l.raw_phones]
        for k, v in info.socials.items():
            l.socials.setdefault(k, v)
        with lock:
            done[0] += 1
            if done[0] % 10 == 0 or done[0] == len(todo):
                log(f"  crawled {done[0]}/{len(todo)} websites")

    if todo:
        log(f"Visiting {len(todo)} websites for emails/phones ...")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for f in as_completed([ex.submit(work, l) for l in todo]):
                try:
                    f.result()
                except Exception as e:  # one broken site must never kill the run
                    log(f"  warning: {type(e).__name__}: {e}")
    finalize(leads, region, check_dns)


def finalize(leads: list[Lead], region: str | None, check_dns: bool = True) -> None:
    """Validate raw emails/phones into the final exported fields."""
    def one(l: Lead) -> None:
        if not l.website:
            l.website_status = "No"
        elif not l.website_status:   # not crawled (--no-website-crawl)
            l.website_status = "Social page only" if is_social(l.website) else "Yes"
        l.phone, l.phone_display, l.phone_type = best_phone(l.raw_phones, region)
        l.email, l.other_emails, l.email_status = verify_emails(l.raw_emails, l.website, check_dns)
    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(one, leads))
