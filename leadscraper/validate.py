"""Email and phone validation / normalisation."""
from __future__ import annotations

import re
import threading
from urllib.parse import unquote

import dns.exception
import dns.resolver
import phonenumbers

EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]{1,64}@([a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)*\.[a-z]{2,24})$")
FILE_EXT = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico", "woff", "woff2",
            "ttf", "eot", "pdf", "mp4", "avif", "map"}
JUNK_DOMAINS = {"example.com", "example.org", "domain.com", "email.com", "yourdomain.com",
                "yoursite.com", "mysite.com", "sentry.io", "wixpress.com", "sentry-next.wixpress.com",
                "test.com", "company.com", "website.com", "godaddy.com", "latofonts.com"}
JUNK_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|mailer-daemon|postmaster|abuse|webmaster|"
                        r"user|name|your-?email|youremail|email|test|admin@admin)$")
ROLE_LOCAL = {"info", "contact", "sales", "support", "hello", "office", "admin", "enquiries",
              "inquiries", "enquiry", "service", "booking", "reservations", "mail", "team"}

_mx_cache: dict[str, str] = {}
_mx_lock = threading.Lock()


def clean_email(raw: str) -> str | None:
    """Return a normalised email or None if it is syntactically junk."""
    e = unquote(raw).strip().strip(".,;:<>()[]\"'").lower()   # mailto links are URL-encoded ("%20info@..")
    e = e.removeprefix("mailto:").split("?")[0].strip()
    m = EMAIL_RE.match(e)
    if not m:
        return None
    local, domain = e.rsplit("@", 1)
    if domain.rsplit(".", 1)[-1] in FILE_EXT or ".." in e:
        return None
    if domain in JUNK_DOMAINS or JUNK_LOCAL.match(local):
        return None
    return e


def check_domain(domain: str, timeout: float = 4.0) -> str:
    """'ok' (MX/A found), 'invalid' (domain does not exist / no mail), 'unknown' (DNS trouble)."""
    with _mx_lock:
        if domain in _mx_cache:
            return _mx_cache[domain]
    res = dns.resolver.Resolver()
    res.lifetime = timeout
    status = "unknown"
    try:
        try:
            ans = res.resolve(domain, "MX")
            # "null MX" (RFC 7505) means the domain accepts no mail
            status = "invalid" if all(str(r.exchange) == "." for r in ans) else "ok"
        except dns.resolver.NoAnswer:
            res.resolve(domain, "A")  # implicit MX
            status = "ok"
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        status = "invalid"
    except (dns.exception.DNSException, OSError):
        status = "unknown"
    with _mx_lock:
        _mx_cache[domain] = status
    return status


def _host(url: str) -> str:
    h = re.sub(r"^[a-z]+://", "", url.lower()).split("/")[0].split(":")[0]
    return h[4:] if h.startswith("www.") else h


def same_site(domain: str, website: str) -> bool:
    h = _host(website)
    return bool(h) and (domain == h or domain.endswith("." + h) or h.endswith("." + domain))


def is_pec(domain: str) -> bool:
    return bool(re.search(r"(^|\.)(pec|legalmail|postecert|arubapec|cert)\.", domain + ".")) or domain.endswith(
        ("pec.it", "legalmail.it", "postecert.it", "arubapec.it"))


def rank_emails(emails: list[str], website: str = "") -> list[str]:
    """Best first: same domain as the business website, then named mailboxes over role ones."""
    def score(e: str) -> tuple[int, int, int]:
        local, domain = e.split("@")
        return (0 if website and same_site(domain, website) else 1,
                1 if is_pec(domain) else 0,      # certified-mail boxes are a poor target for cold email
                1 if local in ROLE_LOCAL else 0)
    return sorted(dict.fromkeys(emails), key=score)


def verify_emails(raw: list[str], website: str = "", check_dns: bool = True) -> tuple[str, list[str], str]:
    """Return (best_email, other_emails, status)."""
    good: list[tuple[str, str]] = []
    for r in raw:
        e = clean_email(r)
        if not e:
            continue
        st = check_domain(e.split("@")[1]) if check_dns else "unknown"
        if st != "invalid":
            good.append((e, st))
    if not good:
        return "", [], ""
    status_of = dict(good)
    ranked = rank_emails([e for e, _ in good], website)
    best = ranked[0]
    label = {"ok": "Valid (domain accepts mail)", "unknown": "Unverified (DNS check unavailable)"}[status_of[best]]
    if website and same_site(best.split("@")[1], website):
        label += ", matches website"
    return best, ranked[1:], label


def normalize_phone(raw: str, region: str | None) -> tuple[str, str, str] | None:
    """Return (E.164, international display, type) for a valid number, else None."""
    try:
        n = phonenumbers.parse(raw.strip(), region.upper() if region else None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(n):
        return None
    t = phonenumbers.number_type(n)
    names = {phonenumbers.PhoneNumberType.MOBILE: "Mobile",
             phonenumbers.PhoneNumberType.FIXED_LINE: "Landline",
             phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: "Landline/Mobile",
             phonenumbers.PhoneNumberType.TOLL_FREE: "Toll-free",
             phonenumbers.PhoneNumberType.VOIP: "VoIP"}
    return (phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164),
            phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
            names.get(t, "Other"))


def best_phone(raws: list[str], region: str | None) -> tuple[str, str, str]:
    for raw in raws:
        for part in re.split(r"[;/|]|\s{2,}|,(?=\s*\+)", raw):
            r = normalize_phone(part, region)
            if r:
                return r
    return "", "", ""


_COUNTRY_ALIASES = {
    "uk": "GB", "united kingdom": "GB", "great britain": "GB", "england": "GB", "scotland": "GB",
    "wales": "GB", "northern ireland": "GB", "uae": "AE", "u.a.e.": "AE", "usa": "US", "u.s.a.": "US",
    "u.s.": "US", "america": "US", "turkey": "TR", "turkiye": "TR", "russia": "RU", "south korea": "KR",
    "korea": "KR", "north korea": "KP", "vietnam": "VN", "iran": "IR", "syria": "SY", "laos": "LA",
    "czech republic": "CZ", "czechia": "CZ", "ivory coast": "CI", "cote d'ivoire": "CI", "bolivia": "BO",
    "venezuela": "VE", "tanzania": "TZ", "moldova": "MD", "palestine": "PS", "taiwan": "TW",
    "hong kong": "HK", "macau": "MO", "kosovo": "XK", "brunei": "BN", "burma": "MM", "myanmar": "MM",
    "ksa": "SA", "saudi": "SA", "holland": "NL", "the netherlands": "NL", "swaziland": "SZ",
}


def country_region(country: str) -> str | None:
    """'Pakistan' -> 'PK'. Accepts names, common aliases (UK, UAE, USA) and 2-letter codes.
    Returns None if unknown or not a region phonenumbers supports."""
    c = country.strip().lower()
    code = _COUNTRY_ALIASES.get(c)
    if not code and len(c) == 2 and c.isalpha():
        code = c.upper()
    if not code:
        try:
            import pycountry
            code = pycountry.countries.lookup(country.strip()).alpha_2
        except (ImportError, LookupError):
            return None
    return code if code in phonenumbers.SUPPORTED_REGIONS else None
