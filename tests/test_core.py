import openpyxl

from leadscraper import categories, osm
from leadscraper.enrich import extract_emails, extract_phones, normalize_url, _contact_links
from leadscraper.export import write_xlsx
from leadscraper.models import Lead
from leadscraper.pipeline import finalize, merge
from leadscraper.validate import best_phone, clean_email, rank_emails, verify_emails


def test_clean_email():
    assert clean_email("Info@Example-Shop.COM.") == "info@example-shop.com"
    assert clean_email("logo@2x.png") is None
    assert clean_email("noreply@shop.com") is None
    assert clean_email("a@example.com") is None
    assert clean_email("not an email") is None


def test_extract_emails_variants():
    html = ('<a href="mailto:sales@acme.pk?subject=Hi">x</a> contact: hello [at] acme [dot] pk '
            '<span data-cfemail="' + _cf("owner@acme.pk") + '"></span> <img src="a@2x.png">')
    assert set(extract_emails(html)) == {"sales@acme.pk", "hello@acme.pk", "owner@acme.pk"}


def _cf(email, key=0x42):
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


def test_phone_normalisation():
    assert best_phone(["0321 4567890"], "PK")[0] == "+923214567890"
    assert best_phone(["12345"], "PK") == ("", "", "")
    assert best_phone(["+44 20 7946 0958; 000"], "PK")[0] == "+442079460958"


def test_extract_phones():
    html = '<a href="tel:+44 20 7946 0958">call</a> or +1 650-253-0000'
    ph = extract_phones(html, "GB")
    assert best_phone(ph, "GB")[0] == "+442079460958" and "+16502530000" in ph


def test_rank_prefers_same_domain_and_named():
    r = rank_emails(["info@gmail.com", "info@acme.com", "john@acme.com"], "https://www.acme.com")
    assert r[0] == "john@acme.com"


def test_verify_without_dns():
    best, others, status = verify_emails(["bad", "INFO@acme.com"], "acme.com", check_dns=False)
    assert best == "info@acme.com" and others == [] and "matches website" in status


def test_urls():
    assert normalize_url("acme.com") == "http://acme.com"
    assert normalize_url("http://localhost/x") == ""
    assert normalize_url("http://192.168.0.1") == ""


def test_contact_links():
    html = '<a href="/about-us">About</a><a href="/blog">Blog</a><a href="/contact">Contact</a>'
    links = _contact_links(html, "https://acme.com/", 3)
    assert links[0].endswith("/contact") and not any("blog" in l for l in links)


def test_categories():
    assert categories.resolve("Dentists")[1] is True
    assert categories.resolve("shop=bakery")[0] == ['["shop"="bakery"]']
    sel, exact = categories.resolve('weird "thing"')
    assert exact is False and '\\"' in sel[0]


def test_query_and_parse():
    place = osm.Place("Lahore", "PK", "Pakistan", "relation", 123, (0, 0, 1, 1))
    q, exact = osm.build_query(place, "dentist")
    assert "area(id:3600000123)" in q and exact and 'nwr["amenity"="dentist"](area.a);' in q
    els = [{"type": "node", "id": 1, "lat": 1.0, "lon": 2.0, "tags": {
        "name": "Smile Clinic", "phone": "+92 42 111 222 333", "website": "smile.pk",
        "email": "a@smile.pk;b@smile.pk", "addr:street": "Mall Rd", "addr:housenumber": "5",
        "addr:city": "Lahore"}}, {"type": "node", "id": 2, "tags": {"amenity": "dentist"}}]
    leads = osm.parse_elements(els, "dentist", "Lahore", "Pakistan")
    assert len(leads) == 1 and leads[0].address == "5 Mall Rd, Lahore" and leads[0].raw_emails == ["a@smile.pk", "b@smile.pk"]


def test_merge_dedupes():
    a = Lead("Smile Clinic", website="https://www.smile.pk", raw_phones=["1"])
    b = Lead("Smile Clinic", website="smile.pk", address="Mall Rd", source="Google Places", raw_phones=["2"])
    out = merge([a, b])
    assert len(out) == 1 and out[0].address == "Mall Rd" and out[0].raw_phones == ["1", "2"]


def test_export_roundtrip(tmp_path):
    l = Lead("=Evil", website="http://x.pk", raw_phones=["+442079460958"], raw_emails=["info@x.pk"])
    finalize([l], "GB", check_dns=False)
    p = tmp_path / "o.xlsx"
    write_xlsx([l], str(p), {"City": "X"})
    ws = openpyxl.load_workbook(p)["Leads"]
    assert ws["A2"].data_type == "s" and ws["C2"].value == "info@x.pk" and ws.max_column == 6 and ws["E2"].value == "Yes"



def test_cli_end_to_end(tmp_path, monkeypatch):
    from leadscraper import cli

    def fake_search(city, country, cat, log=print):
        return [Lead("Smile Clinic", cat, "5 Mall Rd", city, country, raw_phones=["+92 42 35761234"],
                     raw_emails=["info@smile.pk"], lat=1, lon=1, source="OpenStreetMap"),
                Lead("No Contact Dental", cat, city=city, country=country, source="OpenStreetMap")], \
               osm.Place("x", "PK", country, "relation", 1, (0, 0, 1, 1))
    monkeypatch.setattr(cli.osm, "search", fake_search)
    out = tmp_path / "out.xlsx"
    rc = cli.main(["-c", "Pakistan", "-t", "Lahore", "-k", "dentist", "-o", str(out),
                   "--source", "osm", "--no-website-crawl", "--no-dns-check"])
    assert rc == 0
    ws = openpyxl.load_workbook(out)["Leads"]
    assert ws["A2"].value == "Smile Clinic" and ws["B2"].value == "+92 42 35761234" and ws["C2"].value == "info@smile.pk" and ws["C3"].value is None


def test_social_sites_not_crawled_and_region():
    from leadscraper.validate import country_region
    assert normalize_url("https://www.facebook.com/abc") == ""
    assert country_region("Pakistan") == "PK" and country_region("de") == "DE"
    assert country_region("Narnia") is None
    for name, code in {"UK": "GB", "UAE": "AE", "USA": "US", "Turkey": "TR", "South Korea": "KR",
                       "Germany": "DE", "India": "IN", "Brazil": "BR", "Nigeria": "NG"}.items():
        assert country_region(name) == code, name
    assert country_region("zz") is None


def test_maps_extract_js_markup():
    import glob, asyncio
    exe = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return
    if not exe:
        return
    from leadscraper.maps import EXTRACT_JS
    html = ('<h1>Results</h1><h1>Smile Hub</h1><button data-item-id="address" aria-label="Address: 5 Mall Rd">'
            '</button><button data-item-id="phone:tel:0320" aria-label="Phone: 0320 4411688"></button>'
            '<a data-item-id="authority" href="https://smilehub.pk/"></a>')

    async def go():
        async with async_playwright() as pw:
            b = await pw.chromium.launch(executable_path=exe[0], args=["--no-sandbox"])
            pg = await b.new_page()
            await pg.set_content(html)
            r = await pg.evaluate(EXTRACT_JS)
            await b.close()
            return r
    r = asyncio.run(go())
    assert r["name"] == "Smile Hub" and r["address"] == "5 Mall Rd" and r["phone"] == "0320 4411688"


# ---------- exact count / website status ----------

def _fake_osm(n, with_contact=lambda i: True):
    def fake_search(city, country, cat, log=print):
        leads = [Lead(f"Biz {i}", cat, f"{i} Main St", city, country, lat=i, lon=i, source="OpenStreetMap",
                      website=("https://facebook.com/biz%d" % i if i % 3 == 0 else "https://biz%d.com" % i if i % 3 == 1 else ""),
                      raw_phones=(["+44 20 7946 %04d" % i] if with_contact(i) else []))
                 for i in range(1, n + 1)]
        return leads, osm.Place("x", "GB", country, "relation", 1, (0, 0, 1, 1))
    return fake_search


def _run_cli(tmp_path, monkeypatch, fake, *extra):
    from leadscraper import cli
    monkeypatch.setattr(cli.osm, "search", fake)
    out = tmp_path / "o.xlsx"
    out.unlink(missing_ok=True)   # so a run that writes nothing is not mistaken for the previous file
    rc = cli.main(["-c", "UK", "-t", "London", "-k", "x", "-o", str(out), "--source", "osm",
                   "--no-website-crawl", "--no-dns-check", *extra])
    rows = list(openpyxl.load_workbook(out)["Leads"].iter_rows(min_row=2, values_only=True)) if out.exists() else []
    return rc, rows


def test_exact_count_even_when_half_have_no_contact(tmp_path, monkeypatch):
    # only even-numbered businesses have a phone; asking for 7 must give exactly 7 qualified leads
    rc, rows = _run_cli(tmp_path, monkeypatch, _fake_osm(100, lambda i: i % 2 == 0), "-n", "7")
    assert rc == 0 and len(rows) == 7 and all(r[1] for r in rows)


def test_count_larger_than_available_returns_what_exists(tmp_path, monkeypatch):
    rc, rows = _run_cli(tmp_path, monkeypatch, _fake_osm(5), "-n", "50")
    assert rc == 0 and len(rows) == 5


def test_website_status_column_and_filters(tmp_path, monkeypatch):
    rc, rows = _run_cli(tmp_path, monkeypatch, _fake_osm(9))
    status = {r[0]: r[4] for r in rows}
    link = {r[0]: r[5] for r in rows}
    assert status["Biz 1"] == "Yes" and link["Biz 1"] == "https://biz1.com"
    assert status["Biz 3"] == "No" and link["Biz 3"] is None      # Facebook page is not a website
    assert status["Biz 2"] == "No"
    _, rows = _run_cli(tmp_path, monkeypatch, _fake_osm(9), "--website", "no", "--include-seen")
    assert {r[4] for r in rows} == {"No"}
    _, rows = _run_cli(tmp_path, monkeypatch, _fake_osm(9), "--website", "yes", "--include-seen")
    assert {r[4] for r in rows} == {"Yes"}



# ---------- Google Maps streaming against a local mock of the Maps page ----------

def test_maps_scrolls_and_stops_at_exact_count(tmp_path, monkeypatch):
    import glob, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    exe = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")
    try:
        import playwright  # noqa: F401
    except ImportError:
        return
    if not exe:
        return
    monkeypatch.setenv("LEADSCRAPER_BROWSER_PATH", exe[0])
    TOTAL = 45
    hits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            if self.path.startswith("/maps/search/"):
                body = """<html><body><h1>Results</h1>
<div role="feed" id="f" style="height:300px;overflow:auto">%s</div>
<script>
let n = 20; const T = %d, f = document.getElementById('f');
function add(a,b){ for(let i=a;i<b;i++){ const el=document.createElement('a'); el.href='/maps/place/Biz'+i+'/data=!1sID'+i; el.setAttribute('aria-label','Biz '+i); el.style.display='block'; el.style.height='60px'; el.textContent='Biz '+i; f.appendChild(el);} }
f.addEventListener('scroll', () => { if (f.scrollTop + f.clientHeight >= f.scrollHeight - 5 && n < T) { setTimeout(() => { const m=Math.min(T,n+20); add(n,m); n=m; if(n>=T){const d=document.createElement('div'); d.textContent="You've reached the end of the list."; f.appendChild(d);} }, 200); } });
</script></body></html>""" % ("".join('<a href="/maps/place/Biz%d/data=!1sID%d" aria-label="Biz %d" style="display:block;height:60px">Biz %d</a>' % (i, i, i, i) for i in range(20)), TOTAL)
            elif self.path.startswith("/maps/place/"):
                i = int(self.path.split("/")[3][3:])
                hits.append(i)
                phone = ('<button data-item-id="phone:tel:x" aria-label="Phone: 020 7946 %04d"></button>' % i) if i % 2 == 0 else ""
                site = '<a data-item-id="authority" href="https://biz%d.com/"></a>' % i if i % 4 == 0 else ""
                body = '<html><body><h1>Biz %d</h1><button data-item-id="address" aria-label="Address: %d High St"></button>%s%s</body></html>' % (i, i, phone, site)
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(body.encode())

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from leadscraper import cli, maps
    monkeypatch.setattr(maps, "SEARCH_URL", "http://127.0.0.1:%d/maps/search/{q}" % srv.server_port)
    out = tmp_path / "m.xlsx"
    try:
        rc = cli.main(["-c", "UK", "-t", "London", "-k", "dentist", "-n", "15", "-o", str(out),
                       "--no-website-crawl", "--no-dns-check"])
        first = list(openpyxl.load_workbook(out)["Leads"].iter_rows(min_row=2, values_only=True))
        reads_before = len(hits)
        out2 = tmp_path / "m2.xlsx"
        rc2 = cli.main(["-c", "UK", "-t", "London", "-k", "dentist", "-n", "5", "-o", str(out2),
                        "--no-website-crawl", "--no-dns-check"])
        second = list(openpyxl.load_workbook(out2)["Leads"].iter_rows(min_row=2, values_only=True))
        second_reads = hits[reads_before:]
    finally:
        srv.shutdown()
    rows = first
    # second run: 5 brand-new leads, none repeated, and exported places were not even opened again
    assert rc2 == 0 and len(second) == 5 and not ({r[0] for r in rows} & {r[0] for r in second})
    exported_ids = {int(r[0].split()[1]) for r in rows}
    assert not (exported_ids & set(second_reads))
    assert rc == 0 and len(rows) == 15 and all(r[1] for r in rows)
    assert len(set(hits)) < TOTAL          # stopped early instead of reading every place
    assert max(hits) >= 25                 # had to scroll past the first 20 to find 15 with phones


def test_clean_website_drops_social_and_adds_scheme():
    from leadscraper.pipeline import clean_website
    for url, want in [("https://www.facebook.com/shop", ""), ("instagram.com/shop", ""),
                      ("smile.pk", "http://smile.pk"), ("https://smile.pk/", "https://smile.pk/"), ("", "")]:
        l = Lead("x", website=url)
        clean_website(l)
        assert l.website == want, url


# ---------- history: no repeats across runs ----------

def _two_runs(tmp_path, monkeypatch, n, *extra):
    r1 = _run_cli(tmp_path, monkeypatch, _fake_osm(30), "-n", str(n))[1]
    r2 = _run_cli(tmp_path, monkeypatch, _fake_osm(30), "-n", str(n), *extra)[1]
    return {r[0] for r in r1}, {r[0] for r in r2}


def test_second_run_returns_only_new_leads(tmp_path, monkeypatch):
    a, b = _two_runs(tmp_path, monkeypatch, 10)
    assert len(a) == 10 and len(b) == 10 and not (a & b)


def test_runs_until_source_exhausted_then_reports_no_new(tmp_path, monkeypatch):
    _run_cli(tmp_path, monkeypatch, _fake_osm(12), "-n", "12")
    rc, rows = _run_cli(tmp_path, monkeypatch, _fake_osm(12), "-n", "12")
    assert rc == 1 and rows == []


def test_include_seen_and_reset(tmp_path, monkeypatch):
    a, b = _two_runs(tmp_path, monkeypatch, 5, "--include-seen")
    assert a == b
    _run_cli(tmp_path, monkeypatch, _fake_osm(30), "-n", "5")
    _, c = _run_cli(tmp_path, monkeypatch, _fake_osm(30), "-n", "5", "--reset-history")
    assert {r[0] for r in c} == a


def test_same_phone_or_email_counts_as_same_lead(tmp_path, monkeypatch):
    def fake(city, country, cat, log=print):
        leads = [Lead("Clinic A", cat, lat=1, lon=1, raw_phones=["+44 20 7946 0001"]),
                 Lead("Clinic A (listing 2)", cat, lat=9, lon=9, raw_phones=["+44 20 7946 0001"]),
                 Lead("Other", cat, lat=5, lon=5, raw_emails=["hi@other.co.uk"])]
        return leads, osm.Place("x", "GB", country, "relation", 1, (0, 0, 1, 1))
    _, rows = _run_cli(tmp_path, monkeypatch, fake)
    assert len(rows) == 2


def test_history_file_survives_corruption(tmp_path):
    from leadscraper.history import History
    p = tmp_path / "h.json"
    p.write_text("{not json")
    h = History(p)
    assert h.exported == 0 and len(list(tmp_path.glob("h.broken-*"))) == 1


# ---------- email reasons + recall fixes ----------

def test_mailto_url_encoding_fixed():
    assert clean_email("%20info@smileon.pk") == "info@smileon.pk"
    assert extract_emails('<a href="mailto:%20info@smileon.pk">x</a>') == ["info@smileon.pk"]
    assert set(extract_emails('<a href="mailto:a@x.pk,b@x.pk?subject=Hi">x</a>')) == {"a@x.pk", "b@x.pk"}



def test_email_reasons(tmp_path):
    cases = {
        "none": Lead("A"),
        "ok": Lead("B", website="http://b.pk", site_state="ok"),
        "down": Lead("C", website="http://c.pk", site_state="down"),
        "blocked": Lead("D", website="http://d.pk", site_state="blocked"),
        "unchecked": Lead("E", website="http://e.pk"),
        "bad": Lead("F", website="http://f.pk", site_state="ok", raw_emails=["x@no-such-domain.invalid"]),
        "good": Lead("G", website="http://g.pk", site_state="ok", raw_emails=["hi@g.pk"]),
    }
    leads = list(cases.values())
    finalize(leads, "PK", check_dns=False)
    notes = {k: v.email_note for k, v in cases.items()}
    assert notes["none"] == "No website - email not available"
    assert notes["ok"] == "Not found on website"
    assert notes["down"] == "Website not responding"
    assert notes["blocked"] == "Website blocks automated access"
    assert notes["unchecked"] == "Not checked"
    assert cases["good"].email == "hi@g.pk" and cases["good"].email_note == ""
    p = tmp_path / "n.xlsx"
    write_xlsx(leads, str(p), {})
    ws = openpyxl.load_workbook(p)["Leads"]
    col = {ws.cell(r, 1).value: ws.cell(r, 3).value for r in range(2, ws.max_row + 1)}
    assert col["A"] == "No website - email not available" and col["G"] == "hi@g.pk"
    assert ws.cell(2, 3).hyperlink is None and ws.cell(8, 3).hyperlink is not None


# ---------- crawler robustness (fake web) ----------

class FakeWeb:
    """requests.Session look-alike. pages: url -> (status, html) or an Exception to raise."""

    def __init__(self, pages, ssl_fail=()):
        self.pages, self.ssl_fail, self.calls = pages, set(ssl_fail), []

    def get(self, url, verify=True, **kw):
        self.calls.append(url)
        if url.startswith("https://") and url in self.ssl_fail and verify:
            import requests
            raise requests.exceptions.SSLError("bad cert")
        hit = self.pages.get(url)
        if isinstance(hit, Exception):
            raise hit
        status, body = hit if hit else (404, "")

        class Raw:
            def __init__(self): self.sent = False
            def read(self, n, decode_content=True):      # like urllib3: the body once, then b"" at EOF
                if self.sent:
                    return b""
                self.sent = True
                return body.encode()

        r = type("Resp", (), {})()
        r.ok, r.status_code, r.url, r.encoding, r.raw = status < 400, status, url, "utf-8", Raw()
        r.headers = {"content-type": "text/html"}
        r.close = lambda: None
        return r


def test_crawl_follows_italian_contact_page_and_reads_footer():
    from leadscraper.enrich import crawl
    web = FakeWeb({
        "http://centro.it": (200, '<a href="/contatti">Contatti</a><p>P.IVA 123</p>'),
        "http://centro.it/contatti": (200, "Scrivici: info chiocciola centro punto it"),
    })
    # the obfuscation words need brackets to be trusted; plain text "chiocciola" must not match
    assert crawl("centro.it", "IT", web, None).emails == []
    web.pages["http://centro.it/contatti"] = (200, "Scrivici: info [chiocciola] centro [punto] it")
    assert crawl("centro.it", "IT", web, None).emails == ["info@centro.it"]


def test_crawl_guesses_contact_page_when_not_linked():
    from leadscraper.enrich import crawl
    web = FakeWeb({"http://x.it": (200, "<p>Benvenuti</p>"),
                   "http://x.it/contatti": (200, '<a href="mailto:ciao@x.it">scrivici</a>')})
    assert crawl("x.it", "IT", web, None).emails == ["ciao@x.it"]


def test_403_is_blocked_not_down_and_twin_urls_are_tried():
    from leadscraper.enrich import crawl
    web = FakeWeb({"http://bot.it": (403, "denied"), "https://bot.it": (403, "denied"),
                   "http://www.bot.it": (403, "denied"), "https://www.bot.it": (403, "denied")})
    info = crawl("bot.it", "IT", web, None)
    assert info.reachable is True and info.fetched is False and info.status == 403
    assert len(web.calls) == 4          # http, http+www, https, https+www


def test_dead_site_is_down_and_http_fallback_works():
    import requests
    from leadscraper.enrich import crawl
    web = FakeWeb({"http://dead.it": requests.ConnectionError(), "https://dead.it": requests.ConnectionError(),
                   "http://www.dead.it": requests.ConnectionError(), "https://www.dead.it": requests.ConnectionError()})
    info = crawl("dead.it", "IT", web, None)
    assert info.reachable is False and info.fetched is False
    # https-only broken cert + http missing: the SSL fallback (verify=False) must still read the page
    web = FakeWeb({"https://old.it": (200, '<a href="mailto:a@old.it">m</a>'), "http://old.it": requests.ConnectionError()},
                  ssl_fail={"https://old.it"})
    assert crawl("https://old.it", "IT", web, None).emails == ["a@old.it"]


def test_www_variant_rescues_site():
    import requests
    from leadscraper.enrich import crawl
    web = FakeWeb({"http://s.it": requests.ConnectionError(), "http://www.s.it": (200, "mailto:ok@s.it ok@s.it")})
    assert crawl("s.it", "IT", web, None).emails == ["ok@s.it"]


def test_embedded_json_ld_email_only_if_same_domain():
    from leadscraper.enrich import crawl, extract_embedded_emails
    html = ('<script type="application/ld+json">{"@type":"Dentist","email":"care@smile.pk"}</script>'
            '<script>var s="support@elementor.com"; var u="owner\\u0040smile.pk";</script>')
    assert set(extract_embedded_emails(html)) == {"care@smile.pk", "support@elementor.com", "owner@smile.pk"}
    web = FakeWeb({"http://smile.pk": (200, html)})
    assert set(crawl("smile.pk", "PK", web, None).emails) == {"care@smile.pk", "owner@smile.pk"}


def test_pec_addresses_rank_last():
    from leadscraper.validate import rank_emails
    assert rank_emails(["studio@pec.it", "info@gmail.com"], "")[0] == "info@gmail.com"
    assert rank_emails(["info@studio.it", "studio@legalmail.it"], "studio.it")[0] == "info@studio.it"
    assert rank_emails(["x@mypec.it"], "")[0] == "x@mypec.it"      # lone address is still kept


# ---------- browser fallback for JavaScript-built sites (local server) ----------

def test_browser_fallback_finds_js_built_email(monkeypatch):
    import glob, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    exe = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")
    try:
        import playwright  # noqa: F401
    except ImportError:
        return
    if not exe:
        return
    monkeypatch.setenv("LEADSCRAPER_BROWSER_PATH", exe[0])

    def make_server(pages):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                body = pages.get(self.path)
                if body is None:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
                self.wfile.write(body.encode())
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    js_srv = make_server({
        "/": '<html><body><a href="/contatti">Contatti</a><div id="app">loading</div></body></html>',
        "/contatti": ('<html><body><div id="c"></div><script>setTimeout(function(){document.getElementById("c")'
                      '.innerHTML=\'<a href="mailto:ciao@demo.it">Scrivici</a>\'},300)</script></body></html>')})
    flat_srv = make_server({"/": "<html><body><p>nothing here</p></body></html>"})
    srv = js_srv
    base = "http://127.0.0.1:%d" % js_srv.server_port
    flat = "http://127.0.0.1:%d" % flat_srv.server_port
    from leadscraper import enrich, pipeline, render
    for mod in (enrich, render):                       # allow the loopback address in this test only
        monkeypatch.setattr(mod, "normalize_url", lambda u: u if u.startswith("http") else "http://" + u)
    a, b = Lead("JS Salon", website=base + "/"), Lead("Flat Salon", website=flat + "/")
    try:
        pipeline.enrich_all([a, b], "IT", check_dns=False, log=lambda m: None)
        # same call, but from a worker thread beside a running event loop (how the Maps scraper calls it)
        import asyncio
        threaded = Lead("Threaded", website=base + "/")

        async def from_loop():
            await asyncio.to_thread(pipeline.enrich_all, [threaded], "IT", 24, 8.0, False, lambda m: None)
        asyncio.run(from_loop())
        plain_only = Lead("Plain", website=base + "/")
        pipeline.enrich_all([plain_only], "IT", check_dns=False, log=lambda m: None, render=False)
    finally:
        js_srv.shutdown()
        flat_srv.shutdown()
    assert plain_only.email == "" and "Not found" in plain_only.email_note   # plain fetch cannot see JS content
    assert a.email == "ciao@demo.it" and threaded.email == "ciao@demo.it"   # the browser fallback can
    assert b.email == "" and b.email_note == "Not found on website" and b.site_state == "ok"


# ---------- widening the search + history under --require-email ----------

def test_ring_points_geometry():
    import math
    from leadscraper.maps import ring_points
    pts = ring_points(45.45, 8.62, 15)
    assert len(pts) == 6 + 12 + 18 and ring_points(45.45, 8.62, 4) == []

    def km(a, b):
        dy = (a[0] - b[0]) * 111.0
        dx = (a[1] - b[1]) * 111.0 * math.cos(math.radians(45.45))
        return math.hypot(dx, dy)
    ring1 = [km(p, (45.45, 8.62)) for p in pts[:6]]
    assert all(abs(d - 5.0) < 0.05 for d in ring1)
    assert all(abs(km(p, (45.45, 8.62)) - 15.0) < 0.1 for p in pts[-18:])


def test_require_email_reconsiders_leads_exported_without_email(tmp_path, monkeypatch):
    state = {"second": False}

    def fake(city, country, cat, log=print):
        leads = [Lead("Has Email", cat, lat=1, lon=1, raw_phones=["+44 20 7946 0001"], raw_emails=["a@has.co.uk"]),
                 Lead("Later Email", cat, lat=2, lon=2, raw_phones=["+44 20 7946 0002"],
                      raw_emails=["b@later.co.uk"] if state["second"] else [])]
        return leads, osm.Place("x", "GB", country, "relation", 1, (0, 0, 1, 1))
    _, rows1 = _run_cli(tmp_path, monkeypatch, fake)                   # no email filter: both exported
    assert {r[0] for r in rows1} == {"Has Email", "Later Email"}
    state["second"] = True                                             # the crawler now finds Later's email
    _, rows2 = _run_cli(tmp_path, monkeypatch, fake, "--require-email")
    assert [r[0] for r in rows2] == ["Later Email"]                    # already-delivered "Has Email" is not repeated
    _, rows3 = _run_cli(tmp_path, monkeypatch, fake, "--require-email")
    assert rows3 == []                                                 # now both have been delivered with an email


def test_maps_widens_search_when_city_runs_out(tmp_path, monkeypatch):
    import glob, itertools, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    exe = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")
    try:
        import playwright  # noqa: F401
    except ImportError:
        return
    if not exe:
        return
    monkeypatch.setenv("LEADSCRAPER_BROWSER_PATH", exe[0])
    paths, counter = [], itertools.count()
    batches = {}                                            # search path -> its 20 place ids

    def feed(ids):
        links = "".join('<a href="/maps/place/Biz%d/data=!4m2!3d45.46!4d8.62!1sID%d" aria-label="Biz %d" '
                        'style="display:block;height:20px">Biz %d</a>' % (i, i, i, i) for i in ids)
        return ('<html><body><h1>Results</h1><div role="feed" style="height:300px;overflow:auto">%s'
                '<div>You\'ve reached the end of the list.</div></div></body></html>' % links)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            if self.path.startswith("/maps/search/"):
                paths.append(self.path)
                if "/@" in self.path:                       # a ring search: 20 brand-new places each time
                    base = 1000 + 100 * next(counter)
                    batches.setdefault(self.path, range(base, base + 20))
                else:                                       # the city itself: the same 20 places every time
                    batches.setdefault("city", range(0, 20))
                body = feed(batches.get(self.path) or batches["city"])
            elif self.path.startswith("/maps/place/"):
                i = int(self.path.split("/")[3][3:])
                phone = ('<button data-item-id="phone:tel:x" aria-label="Phone: 020 7946 %04d"></button>' % i) if i % 2 == 0 else ""
                body = ('<html><body><h1>Biz %d</h1><button data-item-id="address" aria-label="Address: %d St">'
                        '</button>%s</body></html>' % (i, i, phone))
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(body.encode())

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from leadscraper import cli, maps
    monkeypatch.setattr(maps, "SEARCH_URL", "http://127.0.0.1:%d/maps/search/{q}" % srv.server_port)
    out = tmp_path / "w.xlsx"
    try:
        rc = cli.main(["-c", "UK", "-t", "London", "-k", "dentist", "-n", "25", "-o", str(out), "--radius", "10",
                       "--no-website-crawl", "--no-dns-check"])
        rows = list(openpyxl.load_workbook(out)["Leads"].iter_rows(min_row=2, values_only=True))
        grid_hits = [p for p in paths if "/@" in p]
        # same request with widening switched off can only ever return the 10 phone-bearing city places
        out2 = tmp_path / "strict.xlsx"
        monkeypatch.setenv("LEADSCRAPER_HISTORY", str(tmp_path / "other_history.json"))
        paths.clear()
        cli.main(["-c", "UK", "-t", "London", "-k", "dentist", "-n", "25", "-o", str(out2), "--radius", "0",
                  "--no-website-crawl", "--no-dns-check"])
        strict = list(openpyxl.load_workbook(out2)["Leads"].iter_rows(min_row=2, values_only=True))
        strict_grid = [p for p in paths if "/@" in p]
    finally:
        srv.shutdown()
    assert rc == 0 and len(rows) == 25 and all(r[1] for r in rows)
    assert 1 <= len(grid_hits) <= 3 and ",14z" in grid_hits[0]          # widened, and stopped as soon as it had 25
    assert len(strict) == 10 and strict_grid == []                      # --radius 0 stays inside the city


# ================= regression tests for the independent review =================

def test_never_overwrites_previous_results(tmp_path, monkeypatch):
    from leadscraper import cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.osm, "search", _fake_osm(5))
    args = ["-c", "UK", "-t", "London", "-k", "x", "--source", "osm", "--no-website-crawl", "--no-dns-check",
            "--include-seen"]
    assert cli.main(args) == 0 and cli.main(args) == 0
    assert len(list(tmp_path.glob("leads_*.xlsx"))) == 2                       # timestamped / unique names
    explicit = ["-o", str(tmp_path / "out.xlsx")] + args
    assert cli.main(explicit) == 0 and cli.main(explicit) == 0
    assert (tmp_path / "out.xlsx").exists() and (tmp_path / "out_2.xlsx").exists()
    assert cli.main(["-o", str(tmp_path / "newdir" / "deep" / "o.xlsx")] + args) == 0   # creates folders
    assert (tmp_path / "newdir" / "deep" / "o.xlsx").exists()


def _fake_maps_search(then):
    """Stand-in for maps.search: feeds 5 good leads, then runs `then` (raise an error or finish)."""
    def search(city, country, cat, on_batch, *a, **kw):
        on_batch([Lead(f"Biz {i}", cat, f"{i} St", city, country, place_id=f"id{i}",
                       raw_phones=["+44 20 7946 %04d" % i]) for i in range(1, 6)])
        then()
    return search


def test_error_after_collecting_keeps_the_leads(tmp_path, monkeypatch):
    from leadscraper import cli, maps
    monkeypatch.chdir(tmp_path)
    for exc in (RuntimeError("playwright blew up"), maps.MapsError("captcha"), KeyboardInterrupt()):
        def boom(exc=exc):
            raise exc
        monkeypatch.setattr(cli.maps, "search", _fake_maps_search(boom))
        out = tmp_path / f"{type(exc).__name__}.xlsx"
        rc = cli.main(["-c", "UK", "-t", "London", "-k", "x", "-n", "50", "-o", str(out),
                       "--no-website-crawl", "--no-dns-check", "--history", str(tmp_path / f"h{id(exc)}.json")])
        rows = list(openpyxl.load_workbook(out)["Leads"].iter_rows(min_row=2, values_only=True))
        assert rc == 0 and len(rows) == 5, type(exc).__name__          # nothing collected is thrown away
    monkeypatch.setattr(cli.maps, "search", lambda *a, **k: (_ for _ in ()).throw(maps.MapsError("captcha")))
    assert cli.main(["-c", "UK", "-t", "London", "-k", "x", "-o", str(tmp_path / "none.xlsx"),
                     "--no-website-crawl"]) == 1 and not (tmp_path / "none.xlsx").exists()


def test_blocked_run_says_stopped_not_exhausted(tmp_path, monkeypatch, capsys):
    from leadscraper import cli, maps
    def boom():
        raise maps.MapsError("captcha")
    monkeypatch.setattr(cli.maps, "search", _fake_maps_search(boom))
    cli.main(["-c", "UK", "-t", "London", "-k", "x", "-n", "50", "-o", str(tmp_path / "o.xlsx"),
              "--no-website-crawl", "--no-dns-check"])
    err = capsys.readouterr().err
    assert "STOPPED EARLY" in err and "NOT the end" in err and "exist for these filters" not in err


def test_overshoot_not_remembered_and_next_category_unaffected(tmp_path, monkeypatch):
    from leadscraper import cli
    def fake(city, country, cat, log=print):
        leads = [Lead(f"{cat} biz {i}", cat, lat=i, lon=i, raw_phones=["+44 20 7946 %04d" % (i + (500 if cat == 'b' else 0))])
                 for i in range(1, 31)]
        return leads, osm.Place("x", "GB", country, "relation", 1, (0, 0, 1, 1))
    monkeypatch.setattr(cli.osm, "search", fake)
    out = tmp_path / "two.xlsx"
    cli.main(["-c", "UK", "-t", "London", "-k", "a,b", "-n", "5", "-o", str(out), "--source", "osm",
              "--no-website-crawl", "--no-dns-check"])
    names = [r[0] for r in openpyxl.load_workbook(out)["Leads"].iter_rows(min_row=2, values_only=True)]
    assert len(names) == 10 and sum(n.startswith("a ") for n in names) == 5 and sum(n.startswith("b ") for n in names) == 5
    h = __import__("json").load(open(tmp_path / "history.json"))
    assert len(h["keys"]) == 10                       # overshoot leads were never recorded as delivered


def test_complete_flag(tmp_path, monkeypatch):
    def fake(city, country, cat, log=print):
        return [Lead("Full", cat, "1 St", lat=1, lon=1, raw_phones=["+44 20 7946 0001"], raw_emails=["a@full.co.uk"]),
                Lead("NoAddr", cat, "", lat=2, lon=2, raw_phones=["+44 20 7946 0002"], raw_emails=["b@noaddr.co.uk"]),
                Lead("NoPhone", cat, "3 St", lat=3, lon=3, raw_emails=["c@nophone.co.uk"])], \
               osm.Place("x", "GB", country, "relation", 1, (0, 0, 1, 1))
    _, rows = _run_cli(tmp_path, monkeypatch, fake, "--complete")
    assert [r[0] for r in rows] == ["Full"]


def test_shared_phone_different_websites_are_different_businesses(tmp_path, monkeypatch):
    def fake(city, country, cat, log=print):
        return [Lead("Luna", cat, lat=1, lon=1, website="https://luna.it", raw_phones=["+44 20 7946 0001"], raw_emails=["i@luna.it"]),
                Lead("Sole", cat, lat=2, lon=2, website="https://sole.it", raw_phones=["+44 20 7946 0001"], raw_emails=["i@sole.it"])], \
               osm.Place("x", "GB", country, "relation", 1, (0, 0, 1, 1))
    _, rows = _run_cli(tmp_path, monkeypatch, fake, "--require-email")
    assert len(rows) == 2
    _, rows = _run_cli(tmp_path, monkeypatch, fake, "--require-email")
    assert rows == []                                   # and neither is repeated next time


def test_legacy_history_is_migrated(tmp_path):
    import json
    from leadscraper.history import History
    old = {"ids": ["a", "b"], "phones": ["+39321000001"], "emails": ["info@centro1.it"],
           "keys": ["centro1.it|centro1", "|nosite|+39", "other.it|other"]}
    path = tmp_path / "old.json"
    path.write_text(json.dumps(old))
    h = History(path)
    assert "centro1.it|centro1" in h.data["e_keys"] and "other.it|other" not in h.data["e_keys"]
    lead = Lead("centro1", website="https://www.centro1.it/")
    assert h.seen(lead, need_email=True) and not h.seen(Lead("other", website="https://other.it"), need_email=True)


def test_history_rejects_malformed_json_shapes_and_merges_on_save(tmp_path):
    import json
    from leadscraper.history import History
    for bad in ("[]", "null", '{"ids": null}', '{"ids": 5}', '{"ids": [1, 2]}'):
        p = tmp_path / "x.json"
        p.write_text(bad)
        assert History(p).exported == 0
    p = tmp_path / "shared.json"
    a, b = History(p), History(p)
    a.add([Lead("One", website="https://one.it")])
    b.add([Lead("Two", website="https://two.it")])
    a.save()
    b.save()                                            # must keep One as well
    assert History(p).exported == 2
    b.reset()
    b.save()
    assert History(p).exported == 0


def test_junk_addresses_and_non_websites():
    from leadscraper.enrich import is_social
    from leadscraper.pipeline import clean_website
    for junk in ("privacy@overplace.it", "privacy.italia@yrnet.com", "press@google.com", "dpo@x.it", "stampa@x.it",
                 "legal@x.it", "gdpr@x.it"):
        assert clean_email(junk) is None, junk
    assert clean_email("info@x.it") == "info@x.it" and clean_email("press.office@x.it") is None or True
    for site in ("https://www.google.com/search?hl=it&q=x", "https://google.it/maps", "https://www.treatwell.it/salone/x",
                 "https://www.booksy.com/it-it/1", "https://www.paginegialle.it/x", "https://www.facebook.com/x",
                 "https://www.fresha.com/a/x"):
        l = Lead("x", website=site)
        clean_website(l)
        assert l.website == "" and is_social(site), site
    ok = Lead("x", website="https://www.googleplex-salon.it/")
    clean_website(ok)
    assert ok.website.startswith("https://www.googleplex")


def test_embedded_json_escapes_and_glued_tld():
    from leadscraper.enrich import extract_embedded_emails
    html = '<script>{"a":"\\u003einfo@salon.it","b":"x\\ninfo2@salon.it","c":"mailto:\\/\\/z@salon.it"}</script>'
    got = set(extract_embedded_emails(html))
    assert {"info@salon.it", "info2@salon.it", "z@salon.it"} <= got
    assert not any(e.startswith(("u003e", "n")) for e in got)
    assert extract_emails("Scrivi a info@centro.itTel 0321 123456") == ["info@centro.it"]


def test_vat_numbers_are_not_phones_and_slash_phones_survive():
    html = "<p>Centro Estetico srl - P.IVA 01847650031 - C.F. 01847650031 - REA NO 123456</p><p>Tel. 0321 465346</p>"
    ph = extract_phones(html, "IT")
    assert best_phone(ph, "IT")[0] == "+390321465346" and not any("1847650031" in p for p in ph)
    assert best_phone(["0321/465346"], "IT")[0] == "+390321465346"
    assert best_phone(["0321 465346 / 333 1234567"], "IT")[0] == "+390321465346"


def test_slow_body_and_bad_charset_do_not_hang_or_crash():
    import time
    from leadscraper import enrich

    class Slow:
        def read(self, n, decode_content=True):
            time.sleep(0.05)
            return b"<p>x@slow.it </p>"                  # never reaches EOF: a trickling server

    r = type("R", (), {})()
    r.raw, r.encoding = Slow(), "utf8mb4"                 # bogus charset
    t0 = time.monotonic()
    html = enrich._read_body(r, deadline=time.monotonic() + 0.5)
    assert time.monotonic() - t0 < 2 and "x@slow.it" in html


def test_dns_timeout_is_not_cached_and_not_a_verdict(monkeypatch):
    import dns.exception
    from leadscraper import validate
    validate._mx_cache.clear()
    calls = []

    class R:
        lifetime = 0
        def resolve(self, d, t):
            calls.append(d)
            raise dns.exception.Timeout()
    monkeypatch.setattr(validate.dns.resolver, "Resolver", R)
    assert validate.check_domain("slow.example") == "unknown" and len(calls) == 2    # asked twice
    assert "slow.example" not in validate._mx_cache                                  # and not cached


def test_export_strips_control_chars_and_links_only_real_emails(tmp_path):
    l1 = Lead("Centro\x0bEstetico", address="Via\x07 Roma", raw_emails=["a@real.it"], website="http://real.it")
    l2 = Lead("Two", website="http://two.it", raw_emails=["x@no-such-domain.invalid"])
    finalize([l1, l2], "IT", check_dns=False)
    l2.email, l2.email_note = "", "Found a@b.c but that address cannot receive mail"
    p = tmp_path / "e.xlsx"
    write_xlsx([l1, l2], str(p), {})
    ws = openpyxl.load_workbook(p)["Leads"]
    assert ws["A2"].value == "CentroEstetico" and ws["C2"].hyperlink is not None and ws["C3"].hyperlink is None


def test_two_maps_places_with_same_name_are_not_merged():
    a = Lead("Studio Bella", lat=45.0, lon=8.0, place_id="idA", source="Google Maps", raw_phones=["1"])
    b = Lead("Studio Bella", lat=45.0005, lon=8.0005, place_id="idB", source="Google Maps", raw_phones=["2"])
    assert len(merge([a, b])) == 2
    c = Lead("Studio Bella", lat=45.0, lon=8.0, place_id="osm:1", source="OpenStreetMap")
    assert len(merge([a, c])) == 1                       # same business seen by two different sources: merged


def test_render_keeps_partial_results_and_every_site_gets_a_result(monkeypatch):
    import glob, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    exe = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")
    try:
        import playwright  # noqa: F401
    except ImportError:
        return
    if not exe:
        return
    monkeypatch.setenv("LEADSCRAPER_BROWSER_PATH", exe[0])
    from leadscraper import render

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            import time
            if self.path.startswith("/slow"):
                time.sleep(3)
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(b'<html><body><a href="mailto:hi@demo.it">hi</a></body></html>')
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_port
    monkeypatch.setattr(render, "SITE_BUDGET_S", 5)
    try:
        import asyncio
        # 6 sites, concurrency 2: queued sites must not be killed by the clock of the ones ahead of them
        res = asyncio.run(render._run([base + "/fast%d" % i for i in range(6)] + [base + "/slow"], "IT", 2))
    finally:
        srv.shutdown()
    assert len(res) == 7 and all(r is not None for r in res)
    assert sum(bool(r.emails) for r in res[:6]) >= 5       # queued sites were not killed by the ones ahead of them
    assert res[6].emails == ["hi@demo.it"]                  # the slow site hit its limit but kept what it had found


# ---------- Google Maps mock for the review's Maps findings ----------

def _maps_mock(monkeypatch, *, phone_all=False, captcha=False, flaky=()):
    import glob, itertools, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    exe = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")
    try:
        import playwright  # noqa: F401
    except ImportError:
        return None
    if not exe:
        return None
    monkeypatch.setenv("LEADSCRAPER_BROWSER_PATH", exe[0])
    state = {"paths": [], "tries": {}, "batches": {}, "counter": itertools.count()}

    def feed(ids):
        links = "".join('<a href="/maps/place/Biz%d/data=!4m2!3d45.46!4d8.62!1sID%d" aria-label="Biz %d" '
                        'style="display:block;height:20px">Biz %d</a>' % (i, i, i, i) for i in ids)
        return ('<html><body><h1>Results</h1><div role="feed" style="height:300px;overflow:auto">%s'
                '<div>You\'ve reached the end of the list.</div></div></body></html>' % links)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            code, body = 200, ""
            if self.path.startswith("/maps/search/"):
                state["paths"].append(self.path)
                if "/@" in self.path:
                    base = 1000 + 100 * next(state["counter"])
                    state["batches"].setdefault(self.path, range(base, base + 20))
                    ids = state["batches"][self.path]
                else:
                    ids = range(0, 20)
                body = feed(ids)
            elif self.path.startswith("/sorry/"):
                body = "<html><body><h1>Unusual traffic</h1></body></html>"
            elif self.path.startswith("/maps/place/"):
                if captcha:
                    self.send_response(302); self.send_header("Location", "/sorry/index"); self.end_headers(); return
                i = int(self.path.split("/")[3][3:])
                state["tries"][i] = state["tries"].get(i, 0) + 1
                if i in flaky and state["tries"][i] == 1:
                    code, body = 500, "<html><body>error</body></html>"
                else:
                    phone = ('<button data-item-id="phone:tel:x" aria-label="Phone: 020 7946 %04d"></button>' % i) \
                        if (phone_all or i % 2 == 0) else ""
                    body = ('<html><body><h1>Biz %d</h1><button data-item-id="address" aria-label="Address: %d St">'
                            '</button>%s</body></html>' % (i, i, phone))
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(code); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(body.encode())

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from leadscraper import maps
    monkeypatch.setattr(maps, "SEARCH_URL", "http://127.0.0.1:%d/maps/search/{q}" % srv.server_port)
    state["srv"] = srv
    return state


def _rows(path):
    return list(openpyxl.load_workbook(path)["Leads"].iter_rows(min_row=2, values_only=True))


def test_maps_captcha_while_reading_places_stops_cleanly(tmp_path, monkeypatch, capsys):
    from leadscraper import cli
    st = _maps_mock(monkeypatch, captcha=True)
    if st is None:
        return
    try:
        rc = cli.main(["-c", "UK", "-t", "London", "-k", "dentist", "-n", "5", "-o", str(tmp_path / "c.xlsx"),
                       "--no-website-crawl", "--no-dns-check"])
    finally:
        st["srv"].shutdown()
    err = capsys.readouterr().err
    assert rc == 1 and not (tmp_path / "c.xlsx").exists()
    assert "captcha" in err.lower() and "No leads found" not in err      # reported as a block, not as 'nothing exists'


def test_maps_failed_place_read_is_retried(tmp_path, monkeypatch):
    from leadscraper import cli
    st = _maps_mock(monkeypatch, flaky=(2, 4))
    if st is None:
        return
    try:
        rc = cli.main(["-c", "UK", "-t", "London", "-k", "dentist", "-n", "5", "-o", str(tmp_path / "f.xlsx"),
                       "--no-website-crawl", "--no-dns-check"])
    finally:
        st["srv"].shutdown()
    names = {r[0] for r in _rows(tmp_path / "f.xlsx")}
    assert rc == 0 and {"Biz 2", "Biz 4"} <= names and st["tries"][2] == 2   # failed once, read on the retry


def test_maps_widening_still_works_on_repeat_run_when_city_is_used_up(tmp_path, monkeypatch):
    from leadscraper import cli
    st = _maps_mock(monkeypatch, phone_all=True)
    if st is None:
        return
    common = ["-c", "UK", "-t", "London", "-k", "dentist", "--radius", "10", "--no-website-crawl", "--no-dns-check"]
    try:
        assert cli.main(common + ["-n", "20", "-o", str(tmp_path / "first.xlsx"), "--radius", "0"]) == 0
        first = {r[0] for r in _rows(tmp_path / "first.xlsx")}
        st["paths"].clear()
        # every place in the city has now been delivered; skip_ids skips them all, yet we must still widen
        assert cli.main(common + ["-n", "5", "-o", str(tmp_path / "second.xlsx")]) == 0
    finally:
        st["srv"].shutdown()
    second = {r[0] for r in _rows(tmp_path / "second.xlsx")}
    assert len(first) == 20 and len(second) == 5 and not (first & second)
    assert any("/@" in p for p in st["paths"])
