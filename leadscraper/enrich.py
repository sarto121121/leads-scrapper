"""Crawl a business website for emails, phone numbers and social links."""
from __future__ import annotations

import html as htmllib
import ipaddress
import re
import threading
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import phonenumbers
import requests
import urllib3
from bs4 import BeautifulSoup

from .validate import clean_email, same_site

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9", "Upgrade-Insecure-Requests": "1"}
CONTACT_HINTS = ("contact", "contatt", "kontakt", "contacto", "contato", "impressum", "dove-siamo",
                 "dove siamo", "chi-siamo", "chi siamo", "quienes-somos", "nous-joindre", "contactez",
                 "get-in-touch", "reach", "iletisim", "fale-conosco", "about", "info", "support", "connect")
SOCIALS = {"facebook.com": "facebook", "instagram.com": "instagram", "linkedin.com": "linkedin",
           "twitter.com": "twitter", "x.com": "twitter", "youtube.com": "youtube",
           "tiktok.com": "tiktok", "wa.me": "whatsapp", "api.whatsapp.com": "whatsapp"}
EMAIL_FIND = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}")
OBFUSCATED = re.compile(r"\s*[\[\(\{]\s*(?:at|@|chiocciola|arroba|ät)\s*[\]\)\}]\s*", re.I)
OBFUSCATED_DOT = re.compile(r"\s*[\[\(\{]\s*(?:dot|punto|ponto|point)\s*[\]\)\}]\s*", re.I)
MAX_BYTES = 1_500_000
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
SKIP_HOSTS = {"facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com",
              "youtube.com", "wa.me", "linktr.ee", "business.site", "g.page", "goo.gl", "maps.google.com"}
SOCIAL_HOSTS = {"facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com",
                "youtube.com", "wa.me", "linktr.ee"}
GUESS_PATHS = ("/contatti", "/contact", "/contacts", "/contact-us", "/contattaci", "/kontakt", "/contacto",
               "/contactez-nous", "/chi-siamo", "/about")


class SiteInfo:
    def __init__(self) -> None:
        self.emails: list[str] = []
        self.phones: list[str] = []
        self.socials: dict[str, str] = {}
        self.error: str = ""
        self.reachable: bool = False   # something answered (even an HTTP error such as 403)
        self.fetched: bool = False     # we actually got the home page HTML
        self.robots_blocked: bool = False   # robots.txt forbids reading the home page
        self.status: int | None = None


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
        # a mailto may hold several addresses and is URL-encoded ("mailto:%20info@x.com")
        found += re.split(r"[,;]", unquote(htmllib.unescape(a["href"][7:])).split("?")[0])
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


_UNICODE_AT = re.compile(r"\\u0040|\\x40|&#0*64;|&#x0*40;|&commat;", re.I)


def extract_embedded_emails(html: str) -> list[str]:
    """Emails hidden in scripts / JSON-LD ("email":"info@x.pk"), which visible-text scanning misses.

    These come from machine data, not what a visitor reads, so the caller must only trust the ones
    on the business's own domain (scripts often contain third-party addresses).
    """
    raw = _UNICODE_AT.sub("@", html)
    out: list[str] = []
    for f in EMAIL_FIND.findall(raw):
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


def _candidates(url: str) -> list[str]:
    """The address plus its likely twins: other scheme (http/https) and with/without 'www.'."""
    p = urlparse(url)
    host, port = p.hostname or "", f":{p.port}" if p.port else ""
    hosts = [host, host[4:] if host.startswith("www.") else "www." + host]
    schemes = [p.scheme, "https" if p.scheme == "http" else "http"]
    out: list[str] = []
    for sch in schemes:
        for h in hosts:
            c = p._replace(scheme=sch, netloc=h + port).geturl()
            if c not in out:
                out.append(c)
    return out[:4]


def _get(session: requests.Session, url: str, timeout: float) -> tuple[str, str, int]:
    """Return (html, final_url, status). html is '' for errors / non-HTML. Raises on network failure."""
    kw = dict(timeout=timeout, stream=True, allow_redirects=True)
    try:
        r = session.get(url, **kw)
    except requests.exceptions.SSLError:
        # expired / self-signed certificates are common on small business sites; we only read public pages
        r = session.get(url, verify=False, **kw)
    except requests.exceptions.Timeout:
        r = session.get(url, **{**kw, "timeout": timeout * 1.5})   # slow shared hosting: one patient retry
    try:
        html = ""
        if r.ok and "html" in r.headers.get("content-type", "html").lower():
            body = r.raw.read(MAX_BYTES, decode_content=True)
            html = body.decode(r.encoding or "utf-8", errors="replace")
        return html, r.url, r.status_code
    finally:
        r.close()


def _absorb(info: SiteInfo, html: str, urls: list[str], region: str | None) -> None:
    """Collect emails / phones / socials from one page into info."""
    for e in extract_emails(html):
        if e not in info.emails:
            info.emails.append(e)
    for e in extract_embedded_emails(html):   # machine data: trust only the business's own domain
        if e not in info.emails and any(same_site(e.split("@")[1], u) for u in urls):
            info.emails.append(e)
    for p in extract_phones(html, region):
        if p not in info.phones:
            info.phones.append(p)
    for k, v in extract_socials(html).items():
        info.socials.setdefault(k, v)


def _first_page(url: str, session: requests.Session, robots: Robots | None, timeout: float,
                info: SiteInfo) -> tuple[str, str] | None:
    for cand in _candidates(url):
        if robots and not robots.allowed(cand):
            info.robots_blocked = info.reachable = True   # we respect robots.txt; browser fallback may still read it
            return None
        try:
            html, final, status = _get(session, cand, timeout)
        except requests.RequestException as e:
            info.error = type(e).__name__
            continue
        info.reachable, info.status = True, status
        if html:
            info.error = ""
            info.fetched = True
            return html, final
    return None


def crawl(website: str, region: str | None, session: requests.Session, robots: Robots | None = None,
          timeout: float = 8.0, max_pages: int = 8) -> SiteInfo:
    info = SiteInfo()
    url = normalize_url(website)
    if not url:
        info.error = "invalid url"
        return info
    first = _first_page(url, session, robots, timeout, info)
    if first is None:
        return info
    html, final = first
    urls = [url, final]
    _absorb(info, html, urls, region)
    contacts = _contact_links(html, final, 3)
    guesses = [g for g in (urljoin(final, p) for p in GUESS_PATHS) if g not in contacts]
    tried = {final.rstrip("/"), url.rstrip("/")}
    attempts = 1
    for page in contacts + guesses:
        if attempts >= max_pages:
            break
        if page.rstrip("/") in tried or (page in guesses and info.emails):
            continue   # contact links are always read; blind guesses only while we still have no email
        tried.add(page.rstrip("/"))
        if robots and not robots.allowed(page):
            continue
        attempts += 1
        try:
            html, _, _ = _get(session, page, timeout)
        except requests.RequestException:
            continue
        if html:
            _absorb(info, html, urls, region)
    return info


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=0)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s
