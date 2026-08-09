"""
IPO Guru client -- source of pre-listing data: open/close/allotment/listing
dates, price band, subscription (QIB/NII/Retail/Total), and GMP.

Docs: https://www.ipoguru.in/ipo-gmp-details-developer-api
Free key: email ipoguru.in@gmail.com. Rate limits: 15 req/min, 300 req/day --
respected here by NOT looping over the full list on every request; callers
should fetch once (list of active IPOs) and reuse it for name-matching
rather than issuing one request per company where avoidable.
"""

import difflib
import requests

from .. import config

_session = requests.Session()


class IPOGuruError(Exception):
    pass


def _headers():
    if not config.IPOGURU_API_KEY:
        raise IPOGuruError("IPOGURU_API_KEY is not set")
    return {"X-API-KEY": config.IPOGURU_API_KEY}


def fetch_active_ipos(type_: str | None = None, status: str | None = None) -> list[dict]:
    """GET /ipos -- returns Open/Upcoming/recently-listed IPOs. type_ is
    'mainboard' or 'sme'; status is 'open'/'upcoming'/'closed'. Both optional."""
    params = {}
    if type_:
        params["type"] = type_
    if status:
        params["status"] = status

    resp = _session.get(f"{config.IPOGURU_BASE_URL}/ipos", headers=_headers(), params=params, timeout=15)
    if resp.status_code == 429:
        body = resp.json()
        raise IPOGuruError(f"Rate limited: {body.get('message', body)}")
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise IPOGuruError(f"IPO Guru returned success=false: {data}")
    return data.get("data", [])


def find_by_name(name: str, cutoff: float = 0.6) -> dict | None:
    """Fuzzy-matches `name` against the currently active IPO Guru list
    (both mainboard + SME, all statuses). Only useful for companies that are
    still open/upcoming/recently listed -- IPO Guru doesn't carry deep
    history, so a miss here doesn't mean the company doesn't exist, just
    that it isn't 'active' by IPO Guru's definition."""
    all_ipos = fetch_active_ipos()
    if not all_ipos:
        return None
    names = [ipo["name"] for ipo in all_ipos]
    target = name.strip().lower()
    exact = [ipo for ipo in all_ipos if ipo["name"].strip().lower() == target]
    if exact:
        return exact[0]
    close = difflib.get_close_matches(target, [n.lower() for n in names], n=1, cutoff=cutoff)
    if not close:
        return None
    idx = [n.lower() for n in names].index(close[0])
    return all_ipos[idx]


def _num(v):
    """IPO Guru returns numeric-looking fields as strings; coerce to float,
    passing through None untouched rather than raising on it."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_partial_record(ipo: dict) -> dict:
    """Maps one IPO Guru IPO object onto our ipo_master_records column
    names. Only fields IPO Guru actually knows about are set -- everything
    else is left absent so the caller's merge step doesn't clobber data
    already on file from Indian API / a previous fetch with None."""
    sub = ipo.get("subscription") or {}
    gmp = ipo.get("gmp") or {}

    price_band = ipo.get("price_band")  # e.g. "163-172"
    band_upper = None
    if price_band and "-" in price_band:
        try:
            band_upper = float(price_band.split("-")[-1].strip())
        except ValueError:
            band_upper = None

    return {
        "company_name": ipo.get("name"),
        "issue_category": "Mainboard" if (ipo.get("type") or "").lower() == "mainboard" else "SME",
        "issue_size_cr": _num((ipo.get("issue_size") or "").replace("₹", "").replace("Cr", "").strip() or None),
        "price_band_upper": band_upper if band_upper is not None else _num(ipo.get("issue_price")),
        "subscription_qib": _num(sub.get("qib")),
        "subscription_hni": _num(sub.get("nii")),
        "subscription_rii": _num(sub.get("retail")),
        "subscription_total": _num(sub.get("total")),
        "gmp_percent": _num(gmp.get("percentage")),
        "open_date": ipo.get("open_date"),
        "close_date": ipo.get("close_date"),
        "allotment_date": ipo.get("allotment_date"),
        "listing_date": ipo.get("listing_date"),
        "price_day1": _num(ipo.get("listing_price")),
        "data_source": "ipoguru_live",
    }
