"""Google Maps scraper (drives a real Chromium browser via Playwright). No API key needed.

It searches "<category> in <city>, <country>", scrolls the results list to the end, then opens
every place and reads name, address, phone and website. Google changes its page markup from time
to time, so selectors live in one place (EXTRACT_JS) and are easy to patch.
Scraping Google Maps is against Google's Terms of Service; for official access use --source api.
"""
from __future__ import annotations

import asyncio
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


async def _collect(page, query: str, max_results: int, log) -> list[tuple[str, str]]:
    await page.goto(SEARCH_URL.format(q=quote(query)), wait_until="domcontentloaded", timeout=45000)
    await _accept_consent(page)
    if "/sorry/" in page.url:
        raise MapsError("Google is asking for a captcha (too many requests). Wait a while, or run with "
                        "--show-browser and solve it, or use --source api.")
    try:
        await page.wait_for_selector('div[role="feed"], h1', timeout=20000)
    except Exception:
        return []
    feed = page.locator('div[role="feed"]')
    if not await feed.count():  # a single match jumps straight to the place page
        return [(page.url, "")] if "/maps/place/" in page.url else []
    items: dict[str, str] = {}
    stale = 0
    for _ in range(500):
        links = await page.eval_on_selector_all(
            'div[role="feed"] a[href*="/maps/place/"]',
            "els => els.map(e => [e.href, e.getAttribute('aria-label') || ''])")
        before = len(items)
        for href, name in links:
            items.setdefault(href, name)
        if max_results and len(items) >= max_results:
            break
        if await page.get_by_text(END_TEXT).count():
            break
        stale = stale + 1 if len(items) == before else 0
        if stale >= 6:
            break
        await feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
        await page.wait_for_timeout(1200)
        if len(items) and len(items) % 20 == 0 and len(items) != before:
            log(f"  listed {len(items)} places ...")
    out = list(items.items())
    return out[:max_results] if max_results else out


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


async def _run(queries: list[str], category: str, city: str, country: str, max_results: int,
               headless: bool, concurrency: int, log) -> list[Lead]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise MapsError("Playwright is not installed. Run:  pip install playwright  and then  "
                        "python -m playwright install chromium") from e
    leads: dict[str, Lead] = {}
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=headless)
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
                    found = await _collect(page, q, max_results, log)
                finally:
                    await page.close()
                new = [(u, n) for u, n in found if _place_id(u) not in leads]
                log(f"  -> {len(found)} places listed, reading details of {len(new)} ...")
                sem = asyncio.Semaphore(concurrency)
                tasks = [asyncio.create_task(_detail(ctx, u, sem)) for u, _ in new]
                for i, ((url, listed_name), t) in enumerate(zip(new, tasks), 1):
                    d = await t
                    if i % 20 == 0 or i == len(new):
                        log(f"  read {i}/{len(new)}")
                    if not d or d.get("closed"):
                        continue
                    name = d["name"] or listed_name
                    if not name:
                        continue
                    lead = Lead(name=name, category=category, address=d["address"], city=city,
                                country=country, website=d["website"], source="Google Maps", map_url=url)
                    if d["phone"]:
                        lead.raw_phones.append(d["phone"])
                    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", url)
                    if m:
                        lead.lat, lead.lon = float(m.group(1)), float(m.group(2))
                    leads[_place_id(url)] = lead
        finally:
            await browser.close()
    return list(leads.values())


def _place_id(url: str) -> str:
    m = re.search(r"!1s([^!?&]+)", url) or re.search(r"/maps/place/([^/]+)/", url)
    return m.group(1) if m else url


def search(city: str, country: str, category: str, areas: list[str] | None = None, max_results: int = 0,
           headless: bool = True, concurrency: int = 4, log=print) -> list[Lead]:
    queries = [f"{category} in {a}, {city}, {country}" for a in areas] if areas \
        else [f"{category} in {city}, {country}"]
    leads = asyncio.run(_run(queries, category, city, country, max_results, headless, concurrency, log))
    log(f"  -> {len(leads)} businesses from Google Maps")
    return leads
