"""Google Maps scraper (drives a real Chromium browser via Playwright). No API key needed.

It searches "<category> in <city>, <country>", scrolls the results list, and opens places in batches
(name, address, phone, website), handing each batch to the caller, who can stop it early. Google changes its page markup from time
to time, so selectors live in one place (EXTRACT_JS) and are easy to patch.
Scraping Google Maps is against Google's Terms of Service; for official access use --source api.
"""
from __future__ import annotations

import asyncio
import math
import re
import statistics
from urllib.parse import quote

from .browser import launch_kwargs
from .models import Lead

SEARCH_URL = "https://www.google.com/maps/search/{q}?hl=en"
END_TEXT = "You've reached the end of the list"

EXTRACT_JS = r"""() => {
  const clean = (el, re) => ((el && el.getAttribute('aria-label')) || '').replace(re, '').trim();
  const names = [...document.querySelectorAll('h1')].map(h => h.innerText.trim())
                  .filter(t => t && t !== 'Results');
  const addr = document.querySelector('button[data-item-id="address"]');
  const phone = document.querySelector('button[data-item-id^="phone"]');
  const web = document.querySelector('a[data-item-id="authority"]');
  let phoneVal = clean(phone, /^\s*Phone:\s*/i);
  if (!phoneVal && phone) {
    const id = phone.getAttribute('data-item-id') || '';
    phoneVal = id.replace(/^phone:tel:/, '');
  }
  const closed = /permanently closed/i.test(document.body.innerText.slice(0, 4000));
  return {
    name: names[0] || '',
    address: clean(addr, /^\s*Address:\s*/i),
    phone: phoneVal,
    website: web ? (web.href || '') : '',
    closed: closed,
  };
}"""


CAPTCHA_MSG = ("Google is asking for a captcha (too many requests). Wait a while, or run with "
               "--show-browser and solve it, or use --source api.")


class MapsError(RuntimeError):
    pass


async def _accept_consent(page) -> None:
    for sel in ('button[aria-label="Accept all"]', 'button:has-text("Accept all")',
                'button:has-text("I agree")'):
        try:
            btn = page.locator(sel).first
            if await btn.count():
                await btn.click(timeout=3000)
                await page.wait_for_load_state("domcontentloaded")
                return
        except Exception:
            pass


async def _open_search(page, url: str) -> str:
    """Open a search URL. Returns 'feed' (result list), 'single' (jumped to one place) or 'none'."""
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await _accept_consent(page)
    if "/sorry/" in page.url:
        raise MapsError(CAPTCHA_MSG)
    try:
        # either the results list appears, or Maps jumped straight to the single matching place
        await page.wait_for_function(
            "() => !!document.querySelector('div[role=\"feed\"]') || location.href.includes('/maps/place/')",
            timeout=20000)
    except Exception:
        if "/sorry/" in page.url:
            raise MapsError(CAPTCHA_MSG)
        return "none"
    return "feed" if await page.locator('div[role="feed"]').count() else "single"


async def _scroll_until(page, items: dict[str, str], target: int, log) -> bool:
    """Scroll the results list until it holds `target` places. Returns True once the list has ended."""
    feed = page.locator('div[role="feed"]')
    stale = 0
    for _ in range(500):
        if "/sorry/" in page.url:
            raise MapsError(CAPTCHA_MSG)
        links = await page.eval_on_selector_all(
            'div[role="feed"] a[href*="/maps/place/"]',
            "els => els.map(e => [e.href, e.getAttribute('aria-label') || ''])")
        before = len(items)
        for href, name in links:
            items.setdefault(href, name)
        if len(items) >= target:
            return False
        if await page.get_by_text(END_TEXT).count():
            return True
        stale = stale + 1 if len(items) == before else 0
        if stale >= (6 if items else 15):    # a feed that is still empty gets longer to render
            return True
        await feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
        await page.wait_for_timeout(1200)
    return True


async def _detail(ctx, url: str, sem: asyncio.Semaphore, attempts: int = 2) -> dict | None:
    """Read one place. Retries once; raises MapsError on a captcha; None when it could not be read."""
    async with sem:
        for n in range(attempts):
            page = await ctx.new_page()
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                if "/sorry/" in page.url or (resp is not None and resp.status == 429):
                    raise MapsError(CAPTCHA_MSG)
                if resp is not None and resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}")      # an error page is not a business: retry
                await page.wait_for_selector("h1", timeout=15000)
                try:  # action buttons render a moment after the title
                    await page.wait_for_selector('button[data-item-id="address"], button[data-item-id^="phone"]',
                                                 timeout=4000)
                except Exception:
                    pass
                return await page.evaluate(EXTRACT_JS)
            except MapsError:
                raise
            except Exception:
                await asyncio.sleep(1.5 * (n + 1))
            finally:
                await page.close()
        return None


_COORDS = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")


def ring_points(lat: float, lon: float, radius_km: float, step_km: float = 5.0) -> list[tuple[float, float]]:
    """Search centres on concentric rings around (lat, lon): 6 points at step_km, 12 at 2*step_km, ..."""
    pts: list[tuple[float, float]] = []
    k = 1
    kx = 111.0 * max(0.1, math.cos(math.radians(lat)))      # km per degree of longitude here
    while k * step_km <= radius_km + 1e-9:
        n = 6 * k
        for i in range(n):
            ang = 2 * math.pi * i / n
            pts.append((lat + k * step_km * math.cos(ang) / 111.0, lon + k * step_km * math.sin(ang) / kx))
        k += 1
    return pts


async def _run(queries: list[str], category: str, city: str, country: str, on_batch,
               batch_size: int, headless: bool, concurrency: int, log,
               skip_ids: frozenset[str] = frozenset(), radius_km: float = 0) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise MapsError("Playwright is not installed. Run:  pip install playwright  and then  "
                        "python -m playwright install chromium") from e
    seen_ids: set[str] = set()
    coords: dict[str, tuple[float, float]] = {}     # place id -> (lat, lon), from every place listed
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(**launch_kwargs(headless))
        except Exception as e:
            if "Executable doesn't exist" in str(e):
                raise MapsError("Browser not installed. Run:  python -m playwright install chromium") from e
            raise
        ctx = await browser.new_context(locale="en-US", viewport={"width": 1280, "height": 900})

        async def skip_heavy(route):
            if route.request.resource_type in ("image", "font", "media"):
                await route.abort()
            else:
                await route.continue_()
        await ctx.route("**/*", skip_heavy)

        async def run_one(label: str, url: str) -> bool:
            """Work through one search. Returns True when the caller says it has enough leads."""
            log(f"Google Maps search: {label}")
            page = await ctx.new_page()
            try:
                try:
                    mode = await _open_search(page, url)
                except MapsError:
                    raise
                except Exception as e:      # navigation timeout / network blip: skip this search, keep going
                    log(f"  search skipped ({type(e).__name__}); continuing")
                    return False
                if mode == "none":
                    log("  no results list appeared for this search")
                items: dict[str, str] = {}
                done_urls: set[str] = set()
                ended = mode != "feed"
                if mode == "single":
                    items[page.url] = ""
                while mode != "none":
                    if not ended:
                        ended = await _scroll_until(page, items, len(done_urls) + batch_size, log)
                    for u in items:
                        m = _COORDS.search(u)
                        if m:
                            coords.setdefault(_place_id(u), (float(m.group(1)), float(m.group(2))))
                    fresh = [(u, n) for u, n in items.items() if u not in done_urls]
                    # places already read under an earlier search count as handled
                    done_urls.update(u for u, _ in fresh if _place_id(u) in seen_ids or _place_id(u) in skip_ids)
                    new = [(u, n) for u, n in fresh if u not in done_urls][:batch_size]
                    if not new:
                        if ended:
                            break
                        continue
                    done_urls.update(u for u, _ in new)
                    log(f"  reading {len(new)} places ({len(done_urls)} listed so far) ...")
                    sem = asyncio.Semaphore(concurrency)
                    details = await asyncio.gather(*[_detail(ctx, u, sem) for u, _ in new],
                                                   return_exceptions=True)
                    for d in details:
                        if isinstance(d, MapsError):
                            raise d                      # captcha: stop cleanly, the caller keeps what it has
                    details = [None if isinstance(d, BaseException) else d for d in details]
                    # only places we actually read count as handled; the rest may be retried if listed again
                    seen_ids.update(_place_id(u) for (u, _), d in zip(new, details) if d)
                    unread = sum(d is None for d in details)
                    if unread:
                        log(f"  note: {unread} of {len(new)} places could not be read (slow page); "
                            f"they are retried if they appear in another search")
                    batch = [_to_lead(d, u, n, category, city, country)
                             for (u, n), d in zip(new, details) if d and not d.get("closed")]
                    batch = [b for b in batch if b]
                    # enrichment is slow blocking work: keep it off the browser's event loop
                    if await asyncio.to_thread(on_batch, batch):
                        return True
                return False
            finally:
                await page.close()

        try:
            plan = [(q, SEARCH_URL.format(q=quote(q))) for q in queries]
            widened, i = False, 0
            while i < len(plan):
                label, url = plan[i]
                i += 1
                try:
                    if await run_one(label, url):
                        return
                except MapsError:
                    raise
                except Exception as e:      # one failing search (timeout, page crash) must not end the run
                    log(f"  this search failed midway ({type(e).__name__}: {str(e)[:80]}); moving on")
                if i == len(plan) and radius_km and not widened:
                    widened = True
                    if len(coords) < 3:
                        log("  (cannot widen the search: too few located places to find the city centre)")
                    else:                   # centre of what we found = centre of the city
                        lat = statistics.median(c[0] for c in coords.values())
                        lon = statistics.median(c[1] for c in coords.values())
                        pts = ring_points(lat, lon, radius_km)
                        log(f"City listings used up - widening the search ring by ring (up to {radius_km:g} km "
                            f"around the centre, {len(pts)} extra searches; stops as soon as you have enough).")
                        for la, lo in pts:
                            plan.append((f"{category} around {la:.3f},{lo:.3f}",
                                         SEARCH_URL.format(q=f"{quote(category)}/@{la:.5f},{lo:.5f},14z")))
        finally:
            await browser.close()


def _to_lead(d: dict, url: str, listed_name: str, category: str, city: str, country: str) -> Lead | None:
    name = d["name"] or listed_name
    if not name:
        return None
    lead = Lead(name=name, category=category, address=d["address"], city=city, country=country,
                website=d["website"], source="Google Maps", map_url=url, place_id=_place_id(url))
    if d["phone"]:
        lead.raw_phones.append(d["phone"])
    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", url)
    if m:
        lead.lat, lead.lon = float(m.group(1)), float(m.group(2))
    return lead


def _place_id(url: str) -> str:
    m = re.search(r"!1s([^!?&]+)", url) or re.search(r"/maps/place/([^/]+)/", url)
    return m.group(1) if m else url


def search(city: str, country: str, category: str, on_batch, areas: list[str] | None = None,
           more_queries: bool = False, batch_size: int = 20, headless: bool = True,
           concurrency: int = 4, log=print, skip_ids: frozenset[str] = frozenset(),
           radius_km: float = 0) -> None:
    """Stream results to on_batch(list[Lead]) -> bool. Stop as soon as it returns True.

    more_queries adds differently-worded searches when a target count is not met; radius_km > 0 then
    widens the search ring by ring around the city centre (Maps shows ~120 places per search).
    """
    base = [f"{category} in {a}, {city}, {country}" for a in areas] if areas \
        else [f"{category} in {city}, {country}"]
    if more_queries:
        base += [f"best {category} in {city}, {country}", f"{category} near {city}, {country}"]
    asyncio.run(_run(base, category, city, country, on_batch, batch_size, headless, concurrency, log,
                     skip_ids, radius_km))
