"""
ipoguru.py — KPI fetcher/parser for ipoguru.in, replacing ipogyani.

CONFIRMED (real HTML/DevTools/Network tab seen):
- domain: ipoguru.in
- detail URL: /ipo/{slug} — e.g. /ipo/tempsens-instruments-india-ipo
  for company "Tempsens Instruments (India)". Slug = name lowercased,
  parens dropped (kept as words), non-alnum -> hyphen, + "-ipo".
- KPI block, exact real markup:
    <section id="company-kpis" class="bg-white rounded-2xl p-4
      md:p-8 shadow-sm border border-gray-50">
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div class="bg-gray-50 rounded-xl p-4 border border-gray-100">
          <span class="block text-xs font-bold text-gray-500 uppercase
            tracking-wider mb-1">Pe</span>
          <span class="block text-2xl font-semibold text-gray-900">
            35.38</span>
        </div>
        ... (EPS, ROCE, RONW, Pat Margin tiles, same shape)
- CONFIRMED only 5 KPI fields ever render: PE, EPS, ROCE, RONW,
  PAT MARGIN. No ROE, no debt/equity tile at all (checked Network tab,
  no other KPI request/field). Likely true site-wide, not just this
  company — ROE/debt_equity/ebitda_margin/price_to_book/eps_pre/
  promoter_holding/market_cap are almost certainly NOT available from
  this source. Left mapped below in case some other company's page
  ever shows one, but expect them to stay empty.

STILL UNCONFIRMED:
- Listing-page row/link markup — never seen. Not needed anymore since
  slug is derived straight from company_name (see slugify_candidates()).
  Kept discover_slugs()/resolve_slug_via_listing() as a fallback path
  only, still on the old guessed <a href> scrape — don't trust it,
  primary path below doesn't use it.
- slugify_candidates() only proven correct on ONE real example
  (Tempsens). Punctuation-heavy names (ampersands, "Ltd.", multi-word
  parens) may slugify differently on the real site. fetch_kpi_for_company()
  tries a couple of variants and falls back to discover_slugs() if all
  variants 404 — but only the plain-name variant is proven.
"""

import re
import time
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://ipoguru.in"
LISTING_PATHS = {
    "mainboard": "/ipo-performance",
    "sme": "/sme-ipo-performance",  # still unconfirmed as a real URL
}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ipo-tool-backfill/1.0)"}
REQUEST_DELAY_SEC = 1.0

_slug_cache = {}  # category -> {normalized_name: (slug, real_name)}

# CONFIRMED real labels -> our columns. Rest are guesses, expect empty.
_LABEL_TO_COLUMN = {
    "pe": "pe_ratio",
    "eps": "eps_post",  # site has one EPS tile, not pre/post split
    "roce": "roce",
    "ronw": "ronw",
    "pat margin": "pat_margin",
    # everything below: unconfirmed, likely doesn't exist on this site
    "roe": "roe",
    "debt equity": "debt_equity",
    "debt/equity": "debt_equity",
    "ebitda margin": "ebitda_margin",
    "price to book": "price_to_book",
    "pb ratio": "price_to_book",
    "promoter holding pre issue": "promoter_holding_pre",
    "promoter holding post issue": "promoter_holding_post",
    "market cap": "market_cap",
}


def _normalize_name(name):
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name


def _normalize_label(label):
    label = label.lower().strip()
    label = re.sub(r"[^a-z0-9/ ]", "", label)
    label = re.sub(r"\s+", " ", label)
    return label


def _parse_value(raw):
    """'35.38' -> 35.38, '13.55%' -> 13.55. Non-numeric -> None."""
    if raw is None:
        return None
    cleaned = raw.strip().replace(",", "").rstrip("%").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def slugify_candidates(company_name, issue_category=None):
    """
    Yields candidate slugs, best guess first.

    CONFIRMED real examples:
    - mainboard: "Tempsens Instruments (India)" ->
      "tempsens-instruments-india-ipo"  (suffix "-ipo")
    - sme: "Avience Biomedicals Limited" -> "avience-biomedicals-sme-ipo"
    - sme: "Amba Auto Sales & Services Limited" ->
      "amba-auto-sales-services-sme-ipo"
    So: SME uses suffix "-sme-ipo", mainboard uses "-ipo". "Limited"/
    "Ltd" and "&"/"and" are dropped entirely (not kept as words).
    """
    name = company_name.strip()

    # base with "Limited"/"Ltd" and "&"/"and" stripped, parens flattened
    flat = name.replace("(", " ").replace(")", " ")
    flat = re.sub(r"&", " ", flat)
    flat = re.sub(r"\b(and|limited|ltd|pvt|private)\b", " ", flat, flags=re.I)
    base = re.sub(r"[^a-zA-Z0-9 ]", " ", flat)
    base = re.sub(r"\s+", " ", base).strip().lower()
    core = re.sub(r"\s+", "-", base)

    suffixes = (
        ["-sme-ipo", "-ipo"]
        if (issue_category or "").lower() == "sme"
        else ["-ipo", "-sme-ipo"]
    )
    seen = set()
    for suf in suffixes:
        slug = core + suf
        if slug not in seen:
            seen.add(slug)
            yield slug

    # fallback: parenthetical dropped entirely instead of flattened
    no_parens = re.sub(r"\([^)]*\)", "", name)
    no_parens = re.sub(r"&", " ", no_parens)
    no_parens = re.sub(r"\b(and|limited|ltd|pvt|private)\b", " ", no_parens, flags=re.I)
    base2 = re.sub(r"[^a-zA-Z0-9 ]", " ", no_parens)
    base2 = re.sub(r"\s+", " ", base2).strip().lower()
    core2 = re.sub(r"\s+", "-", base2)
    for suf in suffixes:
        slug2 = core2 + suf
        if slug2 not in seen:
            seen.add(slug2)
            yield slug2


def discover_slugs(category):
    """
    Fallback only — row markup UNCONFIRMED, don't trust as primary
    path. category: 'mainboard' or 'sme'.
    """
    if category in _slug_cache:
        return _slug_cache[category]

    url = BASE_URL + LISTING_PATHS[category]
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    out = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if not text or len(text) < 3:
            continue
        if href.startswith("http") and BASE_URL not in href:
            continue
        if "/ipo/" not in href.lower():
            continue
        slug = href.rstrip("/").split("/")[-1]
        if not slug or slug in ("ipo-performance", "sme-ipo-performance"):
            continue
        key = _normalize_name(text)
        if key:
            out[key] = (slug, text)

    _slug_cache[category] = out
    return out


def resolve_slug_via_listing(company_name, issue_category=None):
    key = _normalize_name(company_name)
    order = (
        ["sme", "mainboard"]
        if (issue_category or "").lower() == "sme"
        else ["mainboard", "sme"]
    )
    for cat in order:
        try:
            m = discover_slugs(cat)
        except requests.RequestException:
            continue
        if key in m:
            return m[key][0]
    return None


def fetch_detail_page(slug):
    """CONFIRMED: /ipo/{slug}."""
    url = f"{BASE_URL}/ipo/{slug}"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def parse_kpi_from_html(html):
    """
    CONFIRMED structure: <section id="company-kpis"> -> tiles
    (div.bg-gray-50.rounded-xl...) -> two spans: label, value.
    """
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    section = soup.find(id="company-kpis")
    if section is None:
        return {}

    result = {}
    for tile in section.select("div.bg-gray-50"):
        spans = tile.find_all("span")
        if len(spans) < 2:
            continue
        label = _normalize_label(spans[0].get_text(strip=True))
        value = _parse_value(spans[1].get_text(strip=True))
        column = _LABEL_TO_COLUMN.get(label)
        if column and value is not None:
            result[column] = value
    return result


def fetch_kpi_for_company(company_name, issue_category=None):
    """
    Primary path: derive slug straight from company_name (no listing
    scrape needed), try a couple of slug variants against the real
    /ipo/{slug} URL. Falls back to the (unconfirmed) listing-page
    scrape only if every slug variant 404s.
    """
    html = None
    for slug in slugify_candidates(company_name):
        time.sleep(REQUEST_DELAY_SEC)
        html = fetch_detail_page(slug)
        if html:
            break

    if not html:
        fallback_slug = resolve_slug_via_listing(company_name, issue_category)
        if fallback_slug:
            time.sleep(REQUEST_DELAY_SEC)
            html = fetch_detail_page(fallback_slug)

    if not html:
        return None  # page not found — distinct from "found, no KPIs"
    return parse_kpi_from_html(html)  # {} here is a real empty KPI section
