# leadscraper

Give it a **country**, a **city** and a **business type** → get an Excel file of leads with
name, phone, email, address and website (plus a "Website Present" Yes/No column).

```bash
pip install -r requirements.txt
python -m leadscraper -c "Pakistan" -t "Lahore" -k "dentist"
python -m leadscraper -c Germany -t Berlin -k "restaurant,cafe" --require-email
python -m leadscraper            # no arguments = interactive prompts
```

Output: `leads_<type>_<city>_<country>.xlsx` (sheet **Leads**, sheet **Summary** with counts).

## How it works
1. **Find businesses** – OpenStreetMap (Nominatim + Overpass), free, worldwide, no key.
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
