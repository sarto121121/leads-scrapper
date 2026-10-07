from __future__ import annotations

import re
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .models import Lead

COLUMNS = [
    ("Name", 34), ("Category", 16), ("Phone", 18), ("Phone Type", 12), ("Email", 32),
    ("Email Status", 30), ("Other Emails", 32), ("Address", 44), ("City", 14), ("Country", 14),
    ("Website Present", 10), ("Website", 34), ("Facebook", 26), ("Instagram", 26),
    ("LinkedIn", 26), ("Map Link", 26), ("Source", 18),
]


def _row(l: Lead) -> list:
    return [l.name, l.category, l.phone_display, l.phone_type, l.email, l.email_status,
            ", ".join(l.other_emails), l.address, l.city, l.country,
            "Yes" if l.has_website else "No", l.website,
            l.socials.get("facebook", ""), l.socials.get("instagram", ""),
            l.socials.get("linkedin", ""), l.map_url, l.source]


def safe_filename(*parts: str) -> str:
    return re.sub(r"[^\w\-]+", "_", "_".join(parts)).strip("_").lower()[:80]


def write_xlsx(leads: list[Lead], path: str, meta: dict[str, str]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append([c for c, _ in COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(vertical="center")
    for i, (_, w) in enumerate(COLUMNS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for l in leads:
        ws.append([_clip(v) for v in _row(l)])
    link_cols = {"Email": "mailto:", "Website": "", "Facebook": "", "Instagram": "",
                 "LinkedIn": "", "Map Link": ""}
    names = [c for c, _ in COLUMNS]
    for col, prefix in link_cols.items():
        idx = names.index(col) + 1
        for r in range(2, ws.max_row + 1):
            c = ws.cell(r, idx)
            if c.value and (prefix or str(c.value).startswith("http")):
                c.hyperlink = prefix + str(c.value)
                c.font = Font(color="0563C1", underline="single")
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions

    s = wb.create_sheet("Summary")
    n = len(leads)
    stats = [
        ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M")), *meta.items(),
        ("Total leads", n),
        ("With email", sum(bool(l.email) for l in leads)),
        ("With phone", sum(bool(l.phone) for l in leads)),
        ("With website", sum(l.has_website for l in leads)),
        ("With email AND phone", sum(bool(l.email and l.phone) for l in leads)),
        ("Note", "Emails are only listed when found on the business's own listing/website and the "
                 "domain accepts mail. Phones are only listed when they are valid for their country."),
    ]
    for k, v in stats:
        s.append([k, v])
        s.cell(s.max_row, 1).font = Font(bold=True)
    s.column_dimensions["A"].width = 24
    s.column_dimensions["B"].width = 100
    wb.save(path)


def _clip(v):
    """Excel cells hold at most 32767 chars; also neutralise formula injection from scraped text."""
    if isinstance(v, str):
        v = v[:32000]
        if v[:1] in ("=", "+", "-", "@") and not v.startswith("+"):
            v = " " + v
    return v
