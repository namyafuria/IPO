"""
IPO Guru KPI fetcher/sync -- companion to fetchers/ipoji.py.

ipoji.com (per project decision, 2026-08-15) is the live-poll source for
GMP/subscription/dates -> ipo_live_tracker. It doesn't carry KPI/financials
data. This module fills that specific gap: Company Financials (Assets/
Total Income/PAT/EBITDA/NetWorth/Borrowings) + Objects-of-Issue, scraped
from ipoguru.in, for every company currently sitting in ipo_live_tracker
that we don't already have KPI data for.

Design mirrors sync_active_ipos() in scheduler.py: reads ipo_live_tracker
(already populated earlier in the same cycle by sync_ipoji_open_ipos()),
diffs against what's already stored, and only fetches what's missing --
so re-running every cycle is cheap (near-zero new fetches once the
backlog is caught up), same "safe to call more than once" property as
the rest of this pipeline.

Robots.txt is honored (site disallows /ipo-archive-index-* -- irrelevant
here since this module never touches archive pages, only /ipo/{slug}
detail pages and, as a fallback, /search).
"""

import re
import time
import logging
import urllib.robotparser
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from .. import db

logger = logging.getLogger("ipo_tool.ipoguru_kpi")

BASE = "https://www.ipoguru.in"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ipo-research-bot/1.0; +personal-research)"}
DELAY_SECONDS = 1.5

_rp = urllib.robotparser.RobotFileParser()
_rp.set_url(f"{BASE}/robots.txt")
try:
    resp = requests.get(f"{BASE}/robots.txt", headers=HEADERS, timeout=5)
    _rp.parse(resp.text.splitlines())
except Exception:
    logger.warning("Could not read ipoguru.in/robots.txt -- proceeding without a robots check this run.")


def _allowed(url: str) -> bool:
    try:
        return _rp.can_fetch(HEADERS["User-Agent"], url)
    except Exception:  # noqa: BLE001
        return True


def _get_soup(url: str):
    if not _allowed(url):
        logger.warning("robots.txt disallows %s -- skipping.", url)
        return None
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    time.sleep(DELAY_SECONDS)
    return BeautifulSoup(resp.text, "html.parser")


# ---------- slug resolution ----------

def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s


def _slug_exists(slug: str) -> bool:
    url = f"{BASE}/ipo/{slug}"
    if not _allowed(url):
        return False
    resp = requests.head(url, headers=HEADERS, timeout=15, allow_redirects=True)
    return resp.status_code == 200


def _find_slug_via_search(name: str):
    search_url = f"{BASE}/search?q={requests.utils.quote(name)}"
    soup = _get_soup(search_url)
    if soup is None:
        return None
    link = soup.find("a", href=re.compile(r"/ipo/"))
    if link is None:
        return None
    return link["href"].rstrip("/").split("/ipo/")[-1]


def resolve_slug(name: str, category: str):
    suffix = "-sme-ipo" if category == "sme" else "-ipo"
    guess = _slugify(name) + suffix
    if _slug_exists(guess):
        return guess
    return _find_slug_via_search(name)


# ---------- KPI table parsing (same approach as the standalone scraper) ----------

def _all_headings(soup):
    return soup.find_all(re.compile(r"^h[1-6]$"))


def _table_after_heading(soup, heading_regex):
    for h in _all_headings(soup):
        if re.search(heading_regex, h.get_text(strip=True), re.I):
            return h.find_next("table")
    return None


def _parse_kv_table(table):
    if table is None:
        return []
    headers = [th.get_text(strip=True) for th in table.find_all("th")]
    if not headers:
        first_row = table.find("tr")
        headers = [td.get_text(strip=True) for td in first_row.find_all("td")] if first_row else []
    rows_out = []
    body_rows = table.find_all("tr")[1:] if table.find("th") else table.find_all("tr")[1:]
    for tr in body_rows:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if not cells:
            continue
        row = {headers[i] if i < len(headers) else f"col{i}": v for i, v in enumerate(cells)}
        rows_out.append(row)
    return rows_out


def scrape_kpi(slug: str) -> dict:
    url = f"{BASE}/ipo/{slug}"
    soup = _get_soup(url)
    if soup is None:
        return {"error": "robots_blocked_or_fetch_failed", "financials": [], "objects_of_issue": []}
    return {
        "financials": _parse_kv_table(_table_after_heading(soup, r"Company Financials")),
        "objects_of_issue": _parse_kv_table(_table_after_heading(soup, r"Objects of the Issue")),
    }


# ---------- ratio math (derived from raw financials line items) ----------

def _parse_period_date(period_str: str):
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(period_str.strip(), fmt)
        except ValueError:
            continue
    return None


def latest_period_values(financials_rows: list) -> dict:
    out = {}
    for row in financials_rows:
        metric = row.get("Metric")
        if not metric:
            continue
        periods = [k for k in row.keys() if k != "Metric"]
        if not periods:
            continue
        dated = [(p, _parse_period_date(p)) for p in periods]
        dated_valid = [(p, d) for p, d in dated if d is not None]
        chosen = max(dated_valid, key=lambda x: x[1])[0] if dated_valid else periods[0]
        cleaned = row[chosen].replace(",", "").replace("₹", "").strip()
        try:
            out[metric] = float(cleaned)
        except ValueError:
            continue
    return out


def compute_ratios(metrics: dict) -> dict:
    ratios = {}
    net_worth = metrics.get("NET Worth")
    pat = metrics.get("Profit After Tax")
    ebitda = metrics.get("EBITDA")
    total_income = metrics.get("Total Income")
    borrowings = metrics.get("Total Borrowing")

    if pat is not None and net_worth:
        ratios["roe"] = round(pat / net_worth * 100, 2)
    if borrowings is not None and net_worth:
        ratios["debt_equity"] = round(borrowings / net_worth, 2)
    if pat is not None and total_income:
        ratios["pat_margin"] = round(pat / total_income * 100, 2)
    if ebitda is not None and total_income:
        ratios["ebitda_margin"] = round(ebitda / total_income * 100, 2)
    return ratios


# ---------- DB plumbing ----------

def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ipo_guru_kpi_raw (
            company_name TEXT PRIMARY KEY,
            matched_slug TEXT,
            financials_json TEXT,
            objects_of_issue_json TEXT,
            linked_at TEXT
        )
    """)


def _backfill_ratios(conn, company_name: str, ratios: dict):
    if not ratios:
        return
    row = conn.execute(
        "SELECT roe, debt_equity, pat_margin, ebitda_margin FROM ipo_master_records "
        "WHERE company_name = ?", (company_name,)
    ).fetchone()
    if row is None:
        return
    row = dict(row) if not isinstance(row, dict) else row
    updates = {}
    if row.get("roe") is None and "roe" in ratios:
        updates["roe"] = ratios["roe"]
    if row.get("debt_equity") is None and "debt_equity" in ratios:
        updates["debt_equity"] = ratios["debt_equity"]
    if row.get("pat_margin") is None and "pat_margin" in ratios:
        updates["pat_margin"] = ratios["pat_margin"]
    if row.get("ebitda_margin") is None and "ebitda_margin" in ratios:
        updates["ebitda_margin"] = ratios["ebitda_margin"]
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE ipo_master_records SET {set_clause} WHERE company_name = ?",
            (*updates.values(), company_name),
        )


def sync_missing_kpis() -> dict:
    """Entry point called from scheduler.py's new Pass 6. Reads
    ipo_live_tracker (must have already been populated this cycle by
    sync_ipoji_open_ipos() -- same ordering dependency sync_active_ipos()
    has), fetches KPI data for any company not already in
    ipo_guru_kpi_raw, stores the raw tables, and backfills the derivable
    ratio columns on ipo_master_records wherever they're still NULL."""
    import json

    conn = db.get_connection()
    try:
        _ensure_table(conn)

        live_rows = [dict(r) for r in conn.execute(
            "SELECT DISTINCT company_name, issue_category FROM ipo_live_tracker"
        ).fetchall()]

        if not live_rows:
            logger.warning(
                "ipo_live_tracker is empty -- nothing for IPO Guru KPI sync this cycle "
                "(expected before sync_ipoji_open_ipos has polled once)."
            )
            return {"fetched": 0, "failed": 0, "skipped_existing": 0}

        existing = {r["company_name"] for r in conn.execute(
            "SELECT company_name FROM ipo_guru_kpi_raw"
        ).fetchall()}

        fetched, failed, skipped = 0, 0, 0

        for row in live_rows:
            name = row.get("company_name")
            if not name:
                continue
            if name in existing:
                skipped += 1
                continue

            category = "sme" if (row.get("issue_category") or "").lower() == "sme" else "mainboard"
            try:
                slug = resolve_slug(name, category)
                if slug is None:
                    logger.warning("IPO Guru KPI sync: no page found for %r", name)
                    failed += 1
                    continue

                kpi = scrape_kpi(slug)
                if kpi.get("error"):
                    logger.warning("IPO Guru KPI sync: %s for %r", kpi["error"], name)
                    failed += 1
                    continue

                conn.execute("""
                    INSERT INTO ipo_guru_kpi_raw
                        (company_name, matched_slug, financials_json, objects_of_issue_json, linked_at)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(company_name) DO UPDATE SET
                        matched_slug=excluded.matched_slug,
                        financials_json=excluded.financials_json,
                        objects_of_issue_json=excluded.objects_of_issue_json,
                        linked_at=excluded.linked_at
                """, (
                    name, slug,
                    json.dumps(kpi["financials"]),
                    json.dumps(kpi["objects_of_issue"]),
                    datetime.now(timezone.utc).isoformat(),
                ))

                ratios = compute_ratios(latest_period_values(kpi["financials"]))
                _backfill_ratios(conn, name, ratios)

                conn.commit()
                fetched += 1
            except Exception as e:  # noqa: BLE001 -- one bad company shouldn't stop the batch
                logger.warning("IPO Guru KPI sync failed for %r: %s", name, e)
                failed += 1

        return {"fetched": fetched, "failed": failed, "skipped_existing": skipped}
    finally:
        conn.close()
