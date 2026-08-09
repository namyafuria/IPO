"""
Central config -- every external credential/setting lives here, read from
environment variables (set these in a .env file locally, or in your host's
dashboard -- Vercel/Render env var settings -- when deployed). Nothing here
is a hardcoded secret.
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # no-op if there's no .env file (e.g. on a host that injects env vars directly)
except ImportError:
    pass

# --- IPO Guru (pre-listing: dates, price band, subscription, GMP) ---
# Get a free key by emailing ipoguru.in@gmail.com with your name, app/company,
# and intended use. 15 req/min, 300 req/day.
IPOGURU_API_KEY = os.environ.get("IPOGURU_API_KEY", "")
IPOGURU_BASE_URL = "https://www.ipoguru.in/api/v1"

# --- Indian API / indianapi.in (post-listing: sector, financials, prices) ---
# Sign up at https://indianapi.in, subscribe to the free tier, grab the key
# from your dashboard. Free/Hobby plans hit stock.indianapi.in.
INDIANAPI_API_KEY = os.environ.get("INDIANAPI_API_KEY", "")
INDIANAPI_BASE_URL = "https://stock.indianapi.in"

# --- Background sync ---
# How often the scheduled job re-checks open/recent IPOs (see scheduler.py).
SYNC_INTERVAL_MINUTES = int(os.environ.get("SYNC_INTERVAL_MINUTES", "60"))

# Days after listing we keep refreshing price_day2/3/5/10 + current_price
# before treating a company as "settled" and leaving it alone.
POST_LISTING_TRACK_DAYS = int(os.environ.get("POST_LISTING_TRACK_DAYS", "12"))


def missing_keys() -> list[str]:
    """Used by /api/health so a misconfigured deployment fails loudly and
    early instead of quietly returning empty live-fetch results."""
    missing = []
    if not IPOGURU_API_KEY:
        missing.append("IPOGURU_API_KEY")
    if not INDIANAPI_API_KEY:
        missing.append("INDIANAPI_API_KEY")
    return missing
