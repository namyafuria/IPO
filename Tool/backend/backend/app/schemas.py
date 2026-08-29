"""
Pydantic models mirroring the ipo_master_records table exactly.

This exists so a newly-fetched company gets stored with the SAME shape as every
row already in ipo_database.db -- no lighter-weight ad hoc entries. See project
plan §62 for the "why".
"""

from typing import Optional
from pydantic import BaseModel


class IPORecord(BaseModel):
    """One row of ipo_master_records. Every field here corresponds 1:1 to a
    real column in the DB (see PRAGMA table_info output from earlier sessions)."""

    company_name: str
    sector: Optional[str] = None
    issue_size_cr: Optional[float] = None
    price_band_upper: Optional[float] = None
    pe_ratio: Optional[float] = None
    roe: Optional[float] = None
    debt_equity: Optional[float] = None
    roce: Optional[float] = None
    ronw: Optional[float] = None
    pat_margin: Optional[float] = None
    ebitda_margin: Optional[float] = None
    price_to_book: Optional[float] = None
    eps_pre: Optional[float] = None
    eps_post: Optional[float] = None
    promoter_holding_pre: Optional[float] = None
    promoter_holding_post: Optional[float] = None
    market_cap: Optional[float] = None
    subscription_qib: Optional[float] = None
    subscription_hni: Optional[float] = None
    subscription_rii: Optional[float] = None
    subscription_total: Optional[float] = None
    gmp_percent: Optional[float] = None
    anchor_allocation_pct: Optional[float] = None
    nifty_trend_pre_listing: Optional[float] = None
    open_date: Optional[str] = None
    close_date: Optional[str] = None
    allotment_date: Optional[str] = None
    listing_date: Optional[str] = None
    listing_day_gain_pct: Optional[float] = None
    price_day1: Optional[float] = None
    price_day2: Optional[float] = None
    price_day3: Optional[float] = None
    price_day5: Optional[float] = None
    price_day10: Optional[float] = None
    nifty_day1: Optional[float] = None
    nifty_day5: Optional[float] = None
    nifty_day10: Optional[float] = None
    data_source: Optional[str] = None
    last_updated: Optional[str] = None
    issue_category: Optional[str] = None  # 'Mainboard' or 'SME'
    name_quality_flag: Optional[str] = None
    isin: Optional[str] = None
    bse_script_code: Optional[str] = None
    nse_symbol: Optional[str] = None
    city: Optional[str] = None
    merchant_banker: Optional[str] = None
    current_price: Optional[float] = None
    current_gain_pct: Optional[float] = None
    current_price_asof: Optional[str] = None


# Every column, in DB order. Single source of truth used by db.py for
# SELECT / INSERT / UPDATE so we never drift from the real schema.
IPO_COLUMNS = list(IPORecord.model_fields.keys())
