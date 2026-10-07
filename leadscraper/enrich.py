"""Crawl a business website for emails, phone numbers and social links."""
from __future__ import annotations

import html as htmllib
import ipaddress
import re
import threading
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import phonenumbers
import requests
from bs4 import BeautifulSoup

from .validate import clean_email

UA = "Mozilla/5.0 (compatible; LeadScraper/1.0; +contact-page-lookup)"
CONTACT_HINTS = ("contact", "kontakt", "contacto", "contato", "contatti", "impressum", "about",
                 "reach", "get-in-touch", "nous-contacter", "iletisim", "support", "connect")
SOCIALS = {"facebook.com": "facebook", "instagram.com": "instagram", "linkedin.com": "linkedin",
           "twitter.com": "twitter", "x.com": "twitter", "youtube.com": "youtube",
           "tiktok.com": "tiktok", "wa.me": "whatsapp", "api.whatsapp.com": "whatsapp"}
EMAIL_FIND = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}")
OBFUSCATED = re.compile(r"\s*[\[\(\{]\s*(?:at|@)\s*[\]\)\}]\s*", re.I)
OBFUSCATED_DOT = re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.I)
MAX_BYTES = 1_500_000
SKIP_HOSTS = {"facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com",
              "youtube.com", "wa.me", "linktr.ee", "business.site", "g.page", "goo.gl", "maps.google.com"}
SOCIAL_HOSTS = {"facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com",
                "youtube.com", "wa.me", "linktr.ee"}
GUESS_PATHS = ("/contact", "/contact-us", "/contact.html", "/contactus", "/about", "/about-us")


class SiteInfo:
    def __init__(self) -> None:
        self.emails: list[str] = []
        self.phones: list[str] = []
        self.socials: dict[str, str] = {}
        self.error: str = ""
        self.reachable: bool = False   # the site's home page answered


class Robots:
    """Per-host robots.txt cache; any fetch problem means 'allowed'."""

    def __init__(self, session: requests.Session, timeout: float):
        self.s, self.t = session, timeout
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        with self._lock:
            known = host in self._parsers
        if not known:
            rp: RobotFileParser | None = None
            try:
                r = self.s.get(host + "/robots.txt", timeout=self.t)
                if r.ok:
                    rp = RobotFileParser()
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp = None
            with self._lock:
                self._parsers[host] = rp
        rp = self._parsers[host]
        return True if rp is None else rp.can_fetch(UA, url)


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if not re.match(r"^[a-z]+://", url, re.I):
        url = "http://" + url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    try:
        ip = ipaddress.ip_address(p.hostname)
        if not ip.is_global:
            return ""
    except ValueError:
        if p.hostname == "localhost" or "." not in p.hostname:
            return ""
    host = p.hostname.lower().removeprefix("www.")
    if host in SKIP_HOSTS or any(host.endswith("." + h) for h in SKIP_HOSTS):
        return ""   # social/redirect pages have no scrapable contact data
    return url


def is_social(url: str) -> bool:
    """True when the 'website' is really a social-media / link-in-bio page."""
    host = (urlparse(url if "://" in url else "//" + url).hostname or "").lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS)


def _decode_cf(enc: str) -> str:
    try:
        key = int(enc[:2], 16)
        return "".join(chr(int(enc[i:i + 2], 16) ^ key) for i in range(2, len(enc), 2))
    except ValueError:
        return ""


def extract_emails(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    found: list[str] = []
    for a in soup.select('a[href^="mailto:" i]'):
        found.append(htmllib.unescape(a["href"][7:]))
    for el in soup.select("[data-cfemail]"):
        found.append(_decode_cf(el["data-cfemail"]))
    for a in soup.select('a[href*="/cdn-cgi/l/email-protection#"]'):
        found.append(_decode_cf(a["href"].split("#", 1)[1]))
    for t in soup(["script", "style"]):
        t.decompose()
    text = htmllib.unescape(soup.get_text(" "))
    text = OBFUSCATED_DOT.sub(".", OBFUSCATED.sub("@", text))
    found += EMAIL_FIND.findall(text)
    out = []
    for f in found:
        e = clean_email(f)
        if e and e not in out:
            out.append(e)
    return out


def extract_phones(html: str, region: str | None) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    found: list[str] = []
    for a in soup.select('a[href^="tel:" i]'):
        found.append(htmllib.unescape(a["href"][4:]).strip())
    for t in soup(["script", "style"]):
        t.decompose()
    text = soup.get_text(" ")
    for m in phonenumbers.PhoneNumberMatcher(text, (region or "").upper() or None,
                                             leniency=phonenumbers.Leniency.VALID):
        found.append(phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164))
    return list(dict.fromkeys(found))


def extract_socials(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for a in BeautifulSoup(html, "lxml").select("a[href]"):
        host = (urlparse(a["href"]).hostname or "").removeprefix("www.")
        net = SOCIALS.get(host)
        path = urlparse(a["href"]).path.strip("/")
        if net and net not in out and path and not path.startswith(("sharer", "share", "intent")):
            out[net] = a["href"]
    return out


def _contact_links(html: str, base: str, limit: int) -> list[str]:
    host = (urlparse(base).hostname or "").removeprefix("www.")
    scored: list[tuple[int, str]] = []
    for a in BeautifulSoup(html, "lxml").select("a[href]"):
        href = urljoin(base, a["href"].split("#")[0])
        p = urlparse(href)
        if p.scheme not in ("http", "https") or (p.hostname or "").removeprefix("www.") != host:
            continue
        blob = (p.path + " " + a.get_text(" ")).lower()
        for i, hint in enumerate(CONTACT_HINTS):
            if hint in blob:
                scored.append((i, href))
                break
    seen, out = set(), []
    for _, h in sorted(scored):
        if h not in seen and h.rstrip("/") != base.rstrip("/"):
            seen.add(h)
            out.append(h)
    return out[:limit]


def _get(session: requests.Session, url: str, timeout: float) -> tuple[str, str, bool]:
    """Return (html, final_url, answered). answered=True for any non-error HTTP response."""
    r = session.get(url, timeout=timeout, stream=True, allow_redirects=True)
    try:
        if not r.ok:
            return "", r.url, False
        if "html" not in r.headers.get("content-type", "html").lower():
            return "", r.url, True
        body = r.raw.read(MAX_BYTES, decode_content=True)
        return body.decode(r.encoding or "utf-8", errors="replace"), r.url, True
    finally:
        r.close()


def crawl(website: str, region: str | None, session: requests.Session, robots: Robots | None = None,
          timeout: float = 8.0, max_pages: int = 6) -> SiteInfo:
    info = SiteInfo()
    url = normalize_url(website)
    if not url:
        info.error = "invalid url"
        return info
    pages = [url]
    visited: set[str] = set()
    guessed: set[str] = set()
    expanded = False
    while pages and len(visited) < max_pages:
        page = pages.pop(0)
        if page in visited or (page in guessed and info.emails):
            continue
        visited.add(page)
        if robots and not robots.allowed(page):
            if not expanded:
                info.reachable = True   # can't verify politely; assume up
            continue
        try:
            html, final, answered = _get(session, page, timeout)
        except requests.RequestException as e:
            if page == url and url.startswith("http://"):
                pages.insert(0, "https://" + url[7:])   # retry on https
            info.error = type(e).__name__
            continue
        if not expanded and answered:
            info.reachable = True
        if not html:
            continue
        info.error = ""
        for e in extract_emails(html):
            if e not in info.emails:
                info.emails.append(e)
        for p in extract_phones(html, region):
            if p not in info.phones:
                info.phones.append(p)
        for k, v in extract_socials(html).items():
            info.socials.setdefault(k, v)
        if not expanded:
            expanded = True
            pages += _contact_links(html, final, max_pages - 1)
            for g in GUESS_PATHS:   # not every site links its contact page in a crawlable way
                gu = urljoin(final, g)
                if gu not in pages:
                    pages.append(gu)
                    guessed.add(gu)
    return info


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en,*;q=0.5"})
    adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=0)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s
