"""
KPI scraper for ipogyani.com.

REVISED 2026-08-28: earlier version guessed slugs via corp-suffix-stripping
and hit /ipo/{slug}. Both wrong per real site confirmed via screenshots:
  - real path is /listed-ipo/{year}/{slug}, not /ipo/{slug}
  - slug suffix is inconsistent ("simca-advertising" has no -ipo suffix,
    "gaja-alternative-ipo" does) -- not derivable from company_name alone
  - name collisions possible (Gaja Capital vs Gaja Alternative)
Fix: never guess a slug. Crawl the real listing pages
(/listed-ipo/{year}?type=mainboard and ?type=sme) once per (year, type),
build a name->slug map from the real <a> links on those pages, and look
up company_name against that map. Falls back to no-match (None) rather
than a blind guess -- caller (backfill_kpi.py) already handles None as
"needs manual slug" via its no_match bucket.

Scope NARROWED 2026-08-28: only pe_ratio and debt_equity are real,
obtainable fields on this page's "Issue & financials" card (confirmed via
DevTools -- see parse_kpi_for_record docstring). roe/ronw/roce/pat_margin/
ebitda_margin/price_to_book/eps_pre_post/promoter_holding_pre_post/
market_cap were an earlier wrong assumption about the table shape and
don't exist here -- not fixable by parsing harder, would need a different
source. Does NOT touch subscription or GMP data -- ipoji.py remains the
sole source for those.
KPI numbers are not currently used by any trained model.
"""

import re
import time
import requests
from bs4 import BeautifulSoup

BASE = "https://ipogyani.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
DELAY_SECONDS = 1.0

_SUFFIX_WORDS = {"limited", "ltd", "llp", "inc", "incorporated", "pvt", "private"}


def _edit_distance_le1(a: str, b: str) -> bool:
    """True if a and b differ by at most one insert/delete/substitute.
    Needed because real DB company_name values have real typos (e.g.
    'Lmited' for 'Limited', confirmed 2026-08-28: Avience Biomedicals
    Lmited) that a plain suffix-string-match silently fails on, wrongly
    reporting a real, fetchable company as a slug no-match."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = sum(1 for x, y in zip(a, b) if x != y)
        return diffs <= 1
    # one char shorter/longer -- check if deleting one char from the
    # longer one gives the shorter one
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    for i in range(len(longer)):
        if longer[:i] + longer[i + 1 :] == shorter:
            return True
    return False


def _normalize_name(name: str) -> str:
    """Corp-suffix stripping, typo-tolerant (see _edit_distance_le1) since
    real DB data has spelling errors on trailing words like 'Limited'.
    Used only for matching company_name to the site's link text -- not
    for guessing a URL."""
    n = name.lower().strip()
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    tokens = n.split(" ")
    while tokens and any(_edit_distance_le1(tokens[-1], w) for w in _SUFFIX_WORDS):
        tokens.pop()
    return " ".join(tokens)


# real link pattern seen on listing pages: /listed-ipo/2026/simca-advertising
# -- kept for discover_listed_slugs()'s <a href> fallback path only.
_LISTING_LINK_RE = re.compile(r"^/listed-ipo/(\d{4})/([a-z0-9-]+)/?$")

# Site is Next.js -- confirmed via real curl output (2026-08-28) that link
# data ships as escaped JSON strings inside <script>self.__next_f.push(...)
# tags (React Server Component flight data). Collapse any run of
# backslashes before a quote down to one quote (handles both single- and
# double-escaped variants) then regex the cleaned text directly.
#
# REVISED 2026-08-28 (2nd pass): the earlier {"@type":"ListItem",
# "url":"...","name":"..."} extraction only caught ~48 of 99 real SME
# 2026 listings -- confirmed via DevTools that "Armour Security India"
# (a real, definitely-listed company) has NO "@type":"ListItem" marker
# and NO "url" field at all in its flight-JSON object. Its real shape is
# {"slug":"armour-security-india","customUrl":null,"year":2026,
# "name":"Armour Security India",...} -- a completely different object
# type (the underlying per-company data record, not SEO ListItem
# markup). Matching on "slug"+"name" directly is both more complete (all
# 99 confirmed present this way) and simpler (no URL path to split).
_ITEM_BLOCK_RE = re.compile(r'\{[^{}]*"slug"\s*:\s*"[a-z0-9-]+"[^{}]*\}')
_SLUG_RE = re.compile(r'"slug"\s*:\s*"([a-z0-9-]+)"')
_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')


def _extract_slug_map_from_flight_json(html: str) -> dict[str, str]:
    """Matches each flat {"slug":"...", ..., "name":"...", ...} object in
    the flight-JSON payload directly (see regex comments above for why
    this replaced the earlier "@type":"ListItem" approach -- that one
    silently dropped roughly half of every listing page's real
    companies, e.g. Armour Security India, since most real entries never
    carry ListItem markup at all)."""
    cleaned = re.sub(r'\\+"', '"', html)
    mapping: dict[str, str] = {}
    for block in _ITEM_BLOCK_RE.finditer(cleaned):
        b = block.group(0)
        sm = _SLUG_RE.search(b)
        nm = _NAME_RE.search(b)
        if sm and nm:
            mapping[_normalize_name(nm.group(1))] = sm.group(1)
    return mapping


def _looks_like_real_company_link_text(text: str) -> bool:
    """Filters out promo-card blurbs ("BEST LISTING OF 2026 Bharat Coking
    Coal +95.7% Coal / Mining - listed 2026-01-19") that also happen to
    wrap a /listed-ipo/{year}/{slug} link -- confirmed real junk seen in
    the fallback <a> scan's output (2026-08-28). A real company-name link
    is short, has no % sign, and isn't a "... - listed ..." sentence."""
    if len(text) > 60:
        return False
    if "%" in text:
        return False
    if " - listed" in text.lower():
        return False
    if "best listing" in text.lower() or "weakest listing" in text.lower():
        return False
    return True


# in-process cache: (year, type_) -> {normalized_name: slug}
_SLUG_CACHE: dict[tuple[int, str], dict[str, str]] = {}


def _get(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            return r.text
        return None
    except requests.RequestException:
        return None


def discover_listed_slugs(year: int, type_: str) -> dict[str, str]:
    """Crawl /listed-ipo/{year}?type={type_} (type_ = 'mainboard' or 'sme')
    and return {normalized_company_name: real_slug} built from the page's
    flight-JSON data records (see _extract_slug_map_from_flight_json).
    Cached per (year, type_) for the process lifetime -- called once per
    unique (year, type) across a whole backfill run, not per company."""
    key = (year, type_)
    if key in _SLUG_CACHE:
        return _SLUG_CACHE[key]

    url = f"{BASE}/listed-ipo/{year}?type={type_}"
    html = _get(url)
    mapping: dict[str, str] = {}
    if html is not None:
        mapping = _extract_slug_map_from_flight_json(html)
        # fallback: in case the site ever serves plain <a href="/listed-ipo/
        # {year}/{slug}"> links (e.g. no-JS variant, or a future site
        # change) -- costs nothing to also check, never overwrites a
        # flight-JSON match
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            m = _LISTING_LINK_RE.match(a["href"])
            if not m:
                continue
            slug = m.group(2)
            text = a.get_text(" ", strip=True)
            if not text or not _looks_like_real_company_link_text(text):
                continue
            mapping.setdefault(_normalize_name(text), slug)
    _SLUG_CACHE[key] = mapping
    return mapping


def resolve_slug(
    company_name: str, year: int, issue_category: str | None = None
) -> str | None:
    """Look up company_name's real slug via discover_listed_slugs(), trying
    both mainboard and sme listing pages for the given year unless
    issue_category narrows it. Returns None (not a guess) on no match."""
    if issue_category and "sme" in issue_category.lower():
        types = ["sme", "mainboard"]
    else:
        types = ["mainboard", "sme"]

    target = _normalize_name(company_name)
    for type_ in types:
        mapping = discover_listed_slugs(year, type_)
        if target in mapping:
            return mapping[target]
    return None


def fetch(year: int, slug: str) -> str | None:
    url = f"{BASE}/listed-ipo/{year}/{slug}"
    return _get(url)


def _rows_of(table) -> list[list[str]]:
    rows = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    return rows


def _clean(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return None if v in ("", "-", "—") else v


def _to_float(v: str | None) -> float | None:
    """Strips units/symbols site puts on every value (%, x, Rs, Cr, commas)
    down to a bare float. Schema columns are all float -- raw strings like
    '39.07%' or 'Rs 116.58 Cr.' can't land there as-is."""
    if v is None:
        return None
    s = re.sub(r"[^\d.\-]", "", v)
    if s in ("", "-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_kpi(slug: str, html: str) -> dict:
    """Flattens every real <table> on the page into {lowercased label:
    [values]}. Confirmed 2026-08-28 via DevTools: only the GMP-history
    section on this page is a real <table> -- the "Issue & financials"
    section is NOT (see _extract_card_rows), so this only ever returns
    GMP-table data now. Kept for that + as a generic fallback in case a
    future page section reverts to real <table> markup."""
    soup = BeautifulSoup(html, "lxml")
    raw: dict[str, list[str]] = {}
    for table in soup.find_all("table"):
        for r in _rows_of(table):
            if len(r) < 2:
                continue
            label = r[0].strip().lower()
            if not label or label == "kpi":
                continue
            raw[label] = [c for c in r[1:]]
    return raw


def _extract_card_rows(html: str, card_title: str) -> dict[str, str]:
    """Extracts stat rows from an ipogyani 'financial card' block. Confirmed
    via DevTools (2026-08-28) these are NOT tables: each card is a
    <div class="bg-card ..."> containing an <h2>{title}</h2> followed by
    one <div class="flex items-center justify-between ..."> row per stat,
    each row holding exactly two direct <span> children (label, value).
    card_title match is case-insensitive substring (e.g. "Issue &
    financials"). Returns {} if no matching card found -- caller treats
    that the same as any other no-data case, doesn't raise."""
    soup = BeautifulSoup(html, "lxml")
    result: dict[str, str] = {}
    for card in soup.find_all("div"):
        classes = card.get("class") or []
        if "bg-card" not in classes:
            continue
        h2 = card.find("h2")
        if not h2 or card_title.lower() not in h2.get_text(strip=True).lower():
            continue
        for row in card.find_all("div"):
            row_classes = row.get("class") or []
            if "flex" not in row_classes or "justify-between" not in row_classes:
                continue
            spans = row.find_all("span", recursive=False)
            if len(spans) != 2:
                continue
            label = spans[0].get_text(strip=True).lower()
            value = spans[1].get_text(strip=True)
            if label:
                result[label] = value
        break  # first matching card only
    return result


def parse_kpi_for_record(slug: str, html: str) -> dict:
    """Maps the 'Issue & financials' card's real fields onto
    ipo_master_records columns. REVISED 2026-08-28: earlier version assumed
    a pre/post-issue table with roe/ronw/roce/pat_margin/ebitda_margin/
    price_to_book/eps_pre_post/promoter_holding_pre_post/market_cap --
    confirmed via DevTools none of that exists on this page. The real
    card only has: Issue Size, Fresh Issue, OFS, IPO PE, Peer/Sector PE,
    PE vs Sector Ratio, Latest EBITDA (Cr), Debt / Equity. Of the columns
    ipo_master_records/KPI_COLUMNS actually wants, only pe_ratio and
    debt_equity are obtainable here -- rest will legitimately stay None,
    not a bug, this site just doesn't publish them on this page. If those
    fields matter, they need a different source entirely, not a fix to
    this parser."""
    card = _extract_card_rows(html, "Issue & financials")

    def val(key):
        return _clean(card.get(key))

    record = {
        "pe_ratio": val("ipo pe"),
        "debt_equity": val("debt / equity"),
    }
    return {k: _to_float(v) for k, v in record.items()}


def fetch_kpi_for_company(
    company_name: str,
    year: int,
    issue_category: str | None = None,
    slug_hint: str | None = None,
) -> dict | None:
    """Convenience wrapper: resolve real slug via discover_listed_slugs()
    (or use slug_hint if already known-good), fetch, parse. Returns None
    if slug can't be resolved OR fetch fails -- caller decides how to
    handle (log for manual review, skip, etc.), this never raises for a
    404 or a no-match."""
    slug = slug_hint or resolve_slug(company_name, year, issue_category)
    if slug is None:
        return None
    html = fetch(year, slug)
    time.sleep(DELAY_SECONDS)
    if html is None:
        return None
    return parse_kpi_for_record(slug, html)
