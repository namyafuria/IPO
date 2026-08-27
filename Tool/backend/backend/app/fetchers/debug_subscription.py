"""
Run this on your machine (needs real network access to ipoji.com).
Pinpoints exactly where subscription parsing breaks for specific slugs.

Usage:
    python debug_subscription.py complete-sports-and-management-ipo esds-software-ipo

If you don't know the exact slug, open the company on ipoji.com and copy
the last path segment from the URL (e.g. ipoji.com/ipo/XXXXX-ipo -> XXXXX-ipo).
"""

import sys
from .ipoji import fetch, parse_subscription, BASE, find_table_by_headers, table_to_rows
from bs4 import BeautifulSoup

for slug in sys.argv[1:]:
    print(f"\n=== {slug} ===")
    url = f"{BASE}/ipo-subscription/{slug}"
    html = fetch(url)
    if html is None:
        print(f"FETCH FAILED entirely for {url} (bad slug, 404, or exhausted retries)")
        continue
    print(f"fetch OK, {len(html)} bytes")

    soup = BeautifulSoup(html, "lxml")
    all_tables = soup.find_all("table")
    print(f"total <table> tags in page: {len(all_tables)}")
    table = find_table_by_headers(soup, ["qib", "total"])
    if not table:
        print("NO TABLE MATCHED headers=['qib','total']")
        for i, t in enumerate(all_tables):
            header_cells = t.find_all(["th", "td"], limit=15)
            header_text = " | ".join(c.get_text(" ", strip=True) for c in header_cells)
            print(f"  table[{i}]: {header_text}")
        # dump raw HTML around first "QIB" occurrence (case-insensitive) so we
        # can see the real markup if it's not inside a <table> at all anymore
        idx = html.lower().find("qib")
        if idx == -1:
            print("literal text 'QIB' not found ANYWHERE in the fetched HTML at all --")
            print(
                "possible causes: page needs JS render, or this html is a loading/placeholder shell."
            )
            print("first 500 chars of html for sanity check:")
            print(html[:500])
        else:
            print(
                f"'QIB' found at char offset {idx} -- raw context (400 chars before/after):"
            )
            print(html[max(0, idx - 400) : idx + 400])
        continue
    print("table matched OK")

    rows = table_to_rows(table)
    print(f"raw rows ({len(rows)}):")
    for r in rows:
        print(" ", r)

    parsed = parse_subscription(slug, html)
    print(f"\nparsed rows ({len(parsed)}):")
    for p in parsed:
        print(" ", p)
    bidding = [p for p in parsed if p["is_bidding_day"]]
    print(f"\nis_bidding_day=True count: {len(bidding)}")
    if not bidding and parsed:
        print(
            "^ rows exist but none matched 'Day N' regex -- print raw as_on values above,"
        )
        print(
            "  that's the real label text ipoji is using -- send it back and I'll fix the regex."
        )
