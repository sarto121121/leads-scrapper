"""Shared Chromium launch settings (Google Maps scraper and the website-rendering fallback)."""
from __future__ import annotations

import os


def launch_kwargs(headless: bool = True) -> dict:
    # LEADSCRAPER_BROWSER_PATH lets you use an already-installed Chrome/Chromium instead of Playwright's
    return dict(headless=headless,
                executable_path=os.environ.get("LEADSCRAPER_BROWSER_PATH") or None,
                args=["--no-sandbox"] if getattr(os, "geteuid", lambda: 1)() == 0 else [])
