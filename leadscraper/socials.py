"""Find a business's own Instagram and LinkedIn profile links on its website (optional extras)."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

NETWORKS = ("instagram", "linkedin")

# profile-looking paths that are NOT a business profile
_IG_RESERVED = {"p", "reel", "reels", "explore", "accounts", "stories", "tv", "direct", "about", "legal",
                "developer", "press", "privacy", "web", "directory", "challenge", "oauth", "emails", "ar",
                "share", "sharer", "embed", "download", "locations", "tags", "popular", "nametag"}
# profiles of the tools/agencies that appear on many sites
_PLATFORM_HANDLES = {"wix", "wordpress", "elementor", "squarespace", "shopify", "godaddy", "weebly", "jimdo",
                     "booksy", "fresha", "treatwell", "google", "instagram", "linkedin", "facebook",
                     "meta", "canva", "mailchimp", "hubspot", "webflow", "divi", "elegantthemes"}
_LI_KINDS = ("company", "in", "school", "showcase")
_IG_RE = re.compile(r"(?:https?:)?//(?:www\.)?instagram\.com/@?([A-Za-z0-9._]{1,30})(?=[/?#\"'\s<>\\)]|$)", re.I)
_LI_RE = re.compile(r"(?:https?:)?//(?:[a-z]{2,3}\.)?linkedin\.com/(company|in|school|showcase)/"
                    r"([A-Za-z0-9_%.\-]{2,100})(?=[/?#\"'\s<>\\)]|$)", re.I)
# a link sitting next to "developed by ...", "design by ..." is the web agency's, not the business's
_CREDIT = re.compile(r"develop|design|power(ed)?\s+by|creat(ed|ion)|realizz|sviluppat|made\s+(by|with)|"
                     r"built\s+(by|with)|site\s+by|web\s+by|credits?|agency|agenzia", re.I)
_GENERIC_WORDS = {"dental", "dentist", "clinic", "clinica", "center", "centre", "centro", "medical", "health",
                  "estetico", "estetica", "beauty", "salon", "studio", "spa", "care", "group", "the", "and",
                  "srl", "llc", "inc", "ltd", "family", "doctor", "dott", "office"}


def instagram_url(handle: str) -> str | None:
    h = handle.strip().lstrip("@").strip("/.").lower()
    if not re.fullmatch(r"[a-z0-9._]{2,30}", h) or h in _IG_RESERVED or h in _PLATFORM_HANDLES:
        return None
    return f"https://www.instagram.com/{h}/"


def linkedin_url(kind: str, slug: str) -> str | None:
    kind, slug = kind.lower(), unquote(slug).strip("/").lower()
    if kind not in _LI_KINDS or not re.fullmatch(r"[a-z0-9_.\-%]{2,100}", slug) or slug in _PLATFORM_HANDLES:
        return None
    return f"https://www.linkedin.com/{kind}/{slug}/"


def normalize(url: str) -> tuple[str, str] | None:
    """(network, clean profile url) for an Instagram/LinkedIn *profile* link, else None."""
    if not url:
        return None
    u = url.strip()
    m = _IG_RE.search(u if "//" in u else "//" + u)
    if m and "instagram.com" in u.lower():
        r = instagram_url(m.group(1))
        return ("instagram", r) if r else None
    m = _LI_RE.search(u if "//" in u else "//" + u)
    if m:
        r = linkedin_url(m.group(1), m.group(2))
        return ("linkedin", r) if r else None
    return None


def extract_candidates(html: str) -> dict[str, list[str]]:
    """Every Instagram/LinkedIn profile link on a page, best placed first (visible links before page data)."""
    found: dict[str, list[str]] = {n: [] for n in NETWORKS}

    def add(url: str) -> None:
        n = normalize(url)
        if n and n[1] not in found[n[0]]:
            found[n[0]].append(n[1])

    soup = BeautifulSoup(html, "lxml")
    for a in soup.select("a[href]"):
        href = a["href"]
        if "instagram.com" not in href.lower() and "linkedin.com" not in href.lower():
            continue
        ctx = " ".join(filter(None, [a.get_text(" ", strip=True)[:60],
                                     a.parent.get_text(" ", strip=True)[:90] if a.parent else ""]))
        if _CREDIT.search(ctx):
            continue
        add(href)
    # schema.org "sameAs" lists and other machine data - scripts and <meta> only, never the anchor links
    # again (those were already judged above, including the "developed by" filter)
    data = " ".join(t.string or "" for t in soup.find_all("script")) + " " + \
        " ".join(str(m.get("content", "")) for m in soup.find_all("meta"))
    for m in _IG_RE.finditer(data):
        add("https://instagram.com/" + m.group(1))
    for m in _LI_RE.finditer(data):
        add(f"https://linkedin.com/{m.group(1)}/{m.group(2)}")
    return found


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in _GENERIC_WORDS]


def _score(handle: str, name: str, host: str) -> int:
    """Longest run of characters a profile handle shares with the business name or website domain."""
    h = re.sub(r"[^a-z0-9]", "", handle.lower())
    label = host.lower().removeprefix("www.").split(".")[0]
    hay = [label, "".join(_tokens(name))] + _tokens(name)
    best = 0
    for t in hay:
        t = re.sub(r"[^a-z0-9]", "", t)
        if t and h:
            best = max(best, SequenceMatcher(None, h, t).find_longest_match(0, len(h), 0, len(t)).size)
    return best


def pick_best(network: str, candidates: list[str], name: str, website: str) -> str:
    """Choose the profile that most resembles the business; ties keep page order."""
    if not candidates:
        return ""
    host = (urlparse(website if "://" in website else "//" + website).hostname or "") if website else ""
    def key(i_c):
        i, c = i_c
        return (-_score(c.rstrip("/").rsplit("/", 1)[-1], name, host), i)
    return min(enumerate(candidates), key=key)[1]


def add_to_lead(lead, candidates: dict[str, list[str]]) -> None:
    """Fill lead.socials (instagram / linkedin) from candidate lists; never overwrite an existing value."""
    for net in NETWORKS:
        if not lead.socials.get(net):
            best = pick_best(net, candidates.get(net, []), lead.name, lead.website)
            if best:
                lead.socials[net] = best
