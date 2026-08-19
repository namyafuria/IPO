"""
bucket_utils.py -- small helper functions for item 3 (predicted vs actual).

Parses the numeric range out of a bucket label (both label styles the
project uses -- e.g. "Gain (0% to 10%)" and "-7.2% to +0.5%") and checks
whether a real observed % move falls inside that range.

No other file in the project needs to change for this one to work -- it
has no imports from the rest of the app, so it's safe to drop into the
app/ folder and import from the new route file.
"""

from __future__ import annotations

import re
from typing import Optional


def _parse_bucket_range(label: str) -> tuple[Optional[float], Optional[float]]:
    """Extracts (low, high) percent bounds from a bucket label.

    Examples:
      "Loss (<-5%)"          -> (None, -5.0)
      "Gain (0% to 10%)"     -> (0.0, 10.0)
      "Strong Gain (10%+)"   -> (10.0, None)
      "-7.2% to +0.5%"       -> (-7.2, 0.5)

    Returns (None, None) if the label doesn't match any known pattern --
    caller should treat that as "can't verify range."
    """
    label = label.strip()

    m = re.search(r"<(-?\d+\.?\d*)%", label)
    if m:
        return (None, float(m.group(1)))

    m = re.search(r"(-?\d+\.?\d*)%\+", label)
    if m:
        return (float(m.group(1)), None)

    m = re.search(r"(-?\d+\.?\d*)%?\s*to\s*[+]?(-?\d+\.?\d*)%", label)
    if m:
        return (float(m.group(1)), float(m.group(2)))

    return (None, None)


def bucket_contains(label: str, actual_pct: float) -> Optional[bool]:
    """True/False if the label's range could be determined and checked,
    None if the label couldn't be parsed (caller should show 'unverified')."""
    low, high = _parse_bucket_range(label)
    if low is None and high is None:
        return None
    if low is not None and actual_pct < low:
        return False
    if high is not None and actual_pct > high:
        return False
    return True
