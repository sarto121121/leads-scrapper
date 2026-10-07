"""Google Maps scraper (drives a real Chromium browser via Playwright). No API key needed.

It searches "<category> in <city>, <country>", scrolls the results list, and opens places in batches
(name, address, phone, website), handing each batch to the caller, who can stop it early. Google changes its page markup from time
to time, so selectors live in one place (EXTRACT_JS) and are easy to patch.
Scraping Google Maps is against Google's Terms of Service; for official access use --source api.
"""
from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import quote

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


async def _open_search(page, query: str) -> str:
    """Open a search. Returns 'feed' (result list), 'single' (jumped to one place) or 'none'."""
    await page.goto(SEARCH_URL.format(q=quote(query)), wait_until="domcontentloaded", timeout=45000)
    await _accept_consent(page)
    if "/sorry/" in page.url:
        raise MapsError("Google is asking for a captcha (too many requests). Wait a while, or run with "
                        "--show-browser and solve it, or use --source api.")
    try:
        await page.wait_for_selector('div[role="feed"], h1', timeout=20000)
    except Exception:
        return "none"
    if await page.locator('div[role="feed"]').count():
        return "feed"
    return "single" if "/maps/place/" in page.url else "none"


async def _scroll_until(page, items: dict[str, str], target: int, log) -> bool:
    """Scroll the results list until it holds `target` places. Returns True once the list has ended."""
    feed = page.locator('div[role="feed"]')
    stale = 0
    for _ in range(500):
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
        if stale >= 6:
            return True
        await feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
        await page.wait_for_timeout(1200)
    return True


async def _detail(ctx, url: str, sem: asyncio.Semaphore) -> dict | None:
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            await page.wait_for_selector("h1", timeout=15000)
            try:  # action buttons render a moment after the title
                await page.wait_for_selector('button[data-item-id="address"], button[data-item-id^="phone"]',
                                             timeout=4000)
            except Exception:
                pass
            return await page.evaluate(EXTRACT_JS)
        except Exception:
            return None
        finally:
            await page.close()


async def _run(queries: list[str], category: str, city: str, country: str, on_batch,
               batch_size: int, headless: bool, concurrency: int, log) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise MapsError("Playwright is not installed. Run:  pip install playwright  and then  "
                        "python -m playwright install chromium") from e
    seen_ids: set[str] = set()
    async with async_playwright() as pw:
        try:
            # LEADSCRAPER_BROWSER_PATH lets you use an already-installed Chrome/Chromium instead
            browser = await pw.chromium.launch(
                headless=headless, executable_path=os.environ.get("LEADSCRAPER_BROWSER_PATH") or None,
                args=["--no-sandbox"] if getattr(os, "geteuid", lambda: 1)() == 0 else [])
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

        try:
            for q in queries:
                log(f"Google Maps search: {q}")
                page = await ctx.new_page()
                try:
                    mode = await _open_search(page, q)
                    items: dict[str, str] = {}
                    done_urls: set[str] = set()
                    ended = mode != "feed"
                    if mode == "single":
                        items[page.url] = ""
                    while mode != "none":
                        if not ended:
                            ended = await _scroll_until(page, items, len(done_urls) + batch_size, log)
                        fresh = [(u, n) for u, n in items.items() if u not in done_urls]
                        # places already read under an earlier search count as handled
                        done_urls.update(u for u, _ in fresh if _place_id(u) in seen_ids)
                        new = [(u, n) for u, n in fresh if u not in done_urls][:batch_size]
                        if not new:
                            if ended:
                                break
                            continue
                        done_urls.update(u for u, _ in new)
                        seen_ids.update(_place_id(u) for u, _ in new)
                        log(f"  reading {len(new)} places ({len(done_urls)} listed so far) ...")
                        sem = asyncio.Semaphore(concurrency)
                        details = await asyncio.gather(*[_detail(ctx, u, sem) for u, _ in new])
                        batch = [_to_lead(d, u, n, category, city, country)
                                 for (u, n), d in zip(new, details) if d and not d.get("closed")]
                        batch = [b for b in batch if b]
                        # enrichment is slow blocking work: keep it off the browser's event loop
                        if await asyncio.to_thread(on_batch, batch):
                            return
                finally:
                    await page.close()
        finally:
            await browser.close()


def _to_lead(d: dict, url: str, listed_name: str, category: str, city: str, country: str) -> Lead | None:
    name = d["name"] or listed_name
    if not name:
        return None
    lead = Lead(name=name, category=category, address=d["address"], city=city, country=country,
                website=d["website"], source="Google Maps", map_url=url)
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
           concurrency: int = 4, log=print) -> None:
    """Stream results to on_batch(list[Lead]) -> bool. Stop as soon as it returns True.

    more_queries adds a few differently-worded searches to dig deeper when a target count is not met.
    """
    base = [f"{category} in {a}, {city}, {country}" for a in areas] if areas \
        else [f"{category} in {city}, {country}"]
    if more_queries:
        base += [f"best {category} in {city}, {country}", f"{category} near {city}, {country}",
                 f"{category} {city} {country} contact"]
    asyncio.run(_run(base, category, city, country, on_batch, batch_size, headless, concurrency, log))
