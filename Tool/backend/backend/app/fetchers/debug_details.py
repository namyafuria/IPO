"""
Usage (from backend\\backend):
  python.exe -m app.fetchers.debug_details <slug-ipo>

Dumps:
  - gmp_daily table: found/not found, raw rows
  - details page: raw context around PE/ROE/Debt/Anchor/GMP keywords,
    so we can fix the regexes to match real ipoji wording
"""
import sys
from .ipoji import fetch, parse_gmp_daily, parse_details_page, BASE, find_table_by_headers, table_to_rows
from bs4 import BeautifulSoup

def dump_context(html, keyword, label):
    idx = html.lower().find(keyword.lower())
    if idx == -1:
        print(f"  [{label}] literal '{keyword}' NOT FOUND anywhere in page")
    else:
        ctx = html[max(0, idx-150):idx+150].replace("\n", " ")
        print(f"  [{label}] found at {idx}: ...{ctx}...")

for slug in sys.argv[1:]:
    print(f"\n=== {slug} ===")

    # --- GMP daily table ---
    gmp_html = fetch(f"{BASE}/ipo-gmp/{slug}")
    if gmp_html is None:
        print("GMP page fetch FAILED")
    else:
        print(f"GMP page fetch OK, {len(gmp_html)} bytes")
        soup = BeautifulSoup(gmp_html, "lxml")
        tables = soup.find_all("table")
        print(f"  <table> tags found: {len(tables)}")
        table = find_table_by_headers(soup, ["date", "gmp"])
        if table:
            rows = table_to_rows(table)
            print(f"  table matched, {len(rows)} raw rows:")
            for r in rows[:6]:
                print("   ", r)
        else:
            print("  NO TABLE matched headers=['date','gmp'] -- dumping all table headers:")
            for i, t in enumerate(tables):
                cells = t.find_all(["th", "td"], limit=15)
                print(f"    table[{i}]: {' | '.join(c.get_text(' ', strip=True) for c in cells)}")
            dump_context(gmp_html, "gmp", "GMP")

    # --- details page ---
    detail_html = fetch(f"{BASE}/ipo/{slug}")
    if detail_html is None:
        print("Details page fetch FAILED")
        continue
    print(f"Details page fetch OK, {len(detail_html)} bytes")
    parsed = parse_details_page(slug, detail_html)
    for field in ["pe_ratio", "roe", "debt_equity", "anchor_allocation", "current_gmp", "issue_size", "price_band"]:
        val = parsed.get(field)
        print(f"  parsed {field}: {val!r}")
        if val is None:
            keyword = {
                "pe_ratio": "PE", "roe": "ROE", "debt_equity": "Debt",
                "anchor_allocation": "Anchor", "current_gmp": "GMP",
                "issue_size": "Issue size", "price_band": "Price band",
            }[field]
            dump_context(detail_html, keyword, field)
