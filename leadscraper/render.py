"""Fallback for websites where plain HTTP finds no email: open them in a real browser.

Many small-business sites build their pages with JavaScript (Wix, Squarespace, page builders) or
refuse non-browser visitors, so the raw HTML has no contact details. A browser sees what a visitor sees.
Only used for leads that still have no email after the normal crawl.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urljoin

from .browser import launch_kwargs
from .enrich import GUESS_PATHS, SiteInfo, UA, _absorb, _candidates, _contact_links, normalize_url
from .models import Lead

PAGE_TIMEOUT_MS = 25000
SITE_BUDGET_S = 90
MAX_PAGES = 5


async def _render_one(ctx, url: str, region: str | None, sem: asyncio.Semaphore) -> SiteInfo:
    info = SiteInfo()
    async with sem:
        page = await ctx.new_page()
        try:
            html, final = "", url
            for cand in _candidates(url):          # http/https and www twins
                try:
                    resp = await page.goto(cand, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                except Exception as e:
                    info.error = type(e).__name__
                    continue
                if resp is not None:
                    info.reachable, info.status = True, resp.status
                if resp is not None and resp.status < 400:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=6000)  # let scripts build the page
                    except Exception:
                        pass
                    html, final = await page.content(), page.url
                    info.fetched = True
                    break
            if not html:
                return info
            urls = [url, final]
            _absorb(info, html, urls, region)
            contacts = _contact_links(html, final, 3)
            guesses = [g for g in (urljoin(final, p) for p in GUESS_PATHS[:4]) if g not in contacts]
            tried = {final.rstrip("/"), url.rstrip("/")}
            for nxt in contacts + guesses:
                if len(tried) >= MAX_PAGES or (info.emails and nxt in guesses):
                    break
                if nxt.rstrip("/") in tried:
                    continue
                tried.add(nxt.rstrip("/"))
                try:
                    resp = await page.goto(nxt, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                    if resp is None or resp.status >= 400:
                        continue
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    _absorb(info, await page.content(), urls, region)
                except Exception:
                    continue
            return info
        finally:
            await page.close()


async def _run(urls: list[str], region: str | None, concurrency: int) -> list[SiteInfo | None]:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs(True))
        try:
            ctx = await browser.new_context(locale="en-US", user_agent=UA, ignore_https_errors=True,
                                            viewport={"width": 1280, "height": 900})

            async def skip_heavy(route):
                if route.request.resource_type in ("image", "font", "media"):
                    await route.abort()
                else:
                    await route.continue_()
            await ctx.route("**/*", skip_heavy)
            sem = asyncio.Semaphore(concurrency)

            async def one(u: str):
                try:
                    return await asyncio.wait_for(_render_one(ctx, u, region, sem), SITE_BUDGET_S)
                except Exception:
                    return None
            return list(await asyncio.gather(*[one(u) for u in urls]))
        finally:
            await browser.close()


def render_missing(leads: list[Lead], region: str | None, concurrency: int = 4, log=print) -> int:
    """Re-read the websites of `leads` in a browser; returns how many gained an email."""
    targets = [(l, normalize_url(l.website)) for l in leads]
    targets = [(l, u) for l, u in targets if u]
    if not targets:
        return 0
    try:
        import playwright  # noqa: F401
    except ImportError:
        log("  (browser fallback skipped: Playwright is not installed)")
        return 0
    log(f"  opening {len(targets)} sites without an email in a real browser ...")
    try:
        results = asyncio.run(_run([u for _, u in targets], region, concurrency))
    except Exception as e:   # a browser problem must never lose the leads we already have
        log(f"  (browser fallback skipped: {type(e).__name__}: {str(e)[:120]})")
        return 0
    gained = 0
    for (l, _), info in zip(targets, results):
        if info is None:
            continue
        had = bool(l.raw_emails)
        l.raw_emails += [e for e in info.emails if e not in l.raw_emails]
        l.raw_phones += [p for p in info.phones[:3] if p not in l.raw_phones]
        for k, v in info.socials.items():
            l.socials.setdefault(k, v)
        if info.fetched:
            l.site_state = "ok"
        gained += bool(l.raw_emails) and not had
    log(f"  browser found emails for {gained} more")
    return gained
