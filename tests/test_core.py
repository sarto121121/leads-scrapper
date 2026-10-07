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
    assert ws["A2"].data_type == "s" and ws["E2"].value == "info@x.pk" and ws["K2"].value == "Yes"


def test_crawl_with_fake_session():
    from leadscraper.enrich import crawl

    pages = {
        "http://shop.example.net": '<a href="/contact-us">Contact</a><a href="https://facebook.com/shop">f</a>',
        "http://shop.example.net/contact-us": 'Mail us: <a href="mailto:hi@shop.example.net">x</a> Tel +44 20 7946 0958',
    }

    class Raw:
        def __init__(self, b): self.b = b.encode()
        def read(self, n, decode_content=True): return self.b

    class Resp:
        def __init__(self, url):
            self.url, self.ok, self.encoding = url, url in pages, "utf-8"
            self.headers = {"content-type": "text/html"}
            self.raw = Raw(pages.get(url, ""))
        def close(self): pass

    class Sess:
        def get(self, url, **kw): return Resp(url)

    info = crawl("shop.example.net", "GB", Sess(), None)
    assert info.emails == ["hi@shop.example.net"] and info.socials["facebook"].endswith("/shop")
    assert any("7946" in p for p in info.phones)


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
                   "--no-website-crawl", "--no-dns-check"])
    assert rc == 0
    ws = openpyxl.load_workbook(out)["Leads"]
    assert ws["A2"].value == "Smile Clinic" and ws["C2"].value == "+92 42 35761234" and ws["K3"].value == "No"
