# leadscraper

Give it a **country**, a **city** and a **business type** → get an Excel file of leads with
name, phone, email, address and website (plus a "Website Present" Yes/No column).

```bash
pip install -r requirements.txt
python -m playwright install chromium        # one time: the browser used for Google Maps
python -m leadscraper -c "Pakistan" -t "Lahore" -k "dentist"
python -m leadscraper -c Pakistan -t Lahore -k dentist --areas "DHA,Gulberg,Johar Town,Model Town"
```

Output: `leads_<type>_<city>_<country>.xlsx` with columns **Name, Phone, Email, Address,
Website Present, Website**. *Website Present* is `Yes` (the link is in the *Website* column) or `No`.
Facebook/Instagram/LinkedIn-style pages are not treated as a website.

### Exact number of leads
```bash
python -m leadscraper -c Pakistan -t Lahore -k dentist -n 50          # exactly 50 leads
python -m leadscraper -c UK -t London -k "wedding venue" -n 100 --require-email
python -m leadscraper -c Pakistan -t Lahore -k dentist -n 30 --website no   # businesses with no website
```
`-n` is per business type and counts only *complete* leads (a phone or an email; use
`--keep-incomplete` to count everything). It keeps scrolling/searching until it has N, then stops.
If fewer than N exist for your filters it saves what exists and tells you how to widen the search
(`--areas`, a bigger city, relaxing `--require-*` / `--website`).

Google Maps shows about 120 results per search; use `--areas` to search neighbourhood by
neighbourhood and get many more. Add `--show-browser` to watch it work or solve a captcha.

## How it works
1. **Find businesses** – default: Google Maps in a real browser (scrolls the whole list, opens every place; no key). Other sources: `--source api` (official Google Places API), `osm`, `all`. OpenStreetMap (Nominatim + Overpass), free, worldwide, no key.
   Optional: set `GOOGLE_MAPS_API_KEY` to also pull Google Places (usually better phone/website
   coverage; max ~60 results per search, billed by Google).
2. **Visit each business website** (home + contact/about pages, robots.txt respected) and extract
   emails (mailto, plain text, `[at]` obfuscation, Cloudflare-protected), phones and social links.
3. **Validate** – emails: syntax, junk filtering, and the domain must actually accept mail (MX/A DNS);
   the best one (matching the business domain, named mailbox first) goes in *Email*, the rest in
   *Other Emails*. Phones: parsed with Google's libphonenumber for that country; only valid numbers
   are kept, shown in international format with type (Mobile/Landline).
4. **Dedupe and export** to formatted Excel (filters, frozen header, clickable links).

## Options
`--limit N`, `--require-email`, `--require-phone`, `--no-website-crawl`, `--no-dns-check`,
`--workers`, `--timeout`, `--source osm|google|auto`, `-o file.xlsx`.
Business type: a keyword (see `leadscraper/categories.py`, ~90 built in), a raw OSM tag such as
`shop=bakery`, or any free text (falls back to a name search).

## Honest limits
- No tool can guarantee "perfect" data. Coverage depends on what the map data and websites publish:
  a business with no website and no listed email will have no email. Nothing is guessed or invented.
  The email check proves the *domain* accepts mail, not that the specific mailbox exists.
- OpenStreetMap coverage varies by country/city (strong in Europe, weaker in some regions).
  Add a Google key for more complete results.
- Be polite: the tool rate-limits geocoding and respects robots.txt. Use leads in line with
  local anti-spam/data-protection law (GDPR, CAN-SPAM, PECR, etc.).

## Tests
`python -m pytest tests`
