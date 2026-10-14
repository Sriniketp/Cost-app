"""HMT GDP deflator lookup for converting monetary outputs to a chosen price base.

Deflators are fetched from the gov.uk "GDP deflators at market prices, and
money GDP" publication (HM Treasury), which is refreshed quarterly. The latest
publication is discovered dynamically so the app always uses current data
without a manual update.
"""

from __future__ import annotations

import re
from io import BytesIO

import numpy as np
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The gov.uk "collection" page lists every historical publication of the GDP
# deflator series. We always resolve the first (most recent) entry from here.
_COLLECTION_URL = (
    "https://www.gov.uk/government/collections/"
    "gdp-deflators-at-market-prices-and-money-gdp"
)

# Used to pull "<Month> <Year>" (e.g. "June 2026") out of the publication
# page's title, so the UI can show which release the deflators came from.
_MONTH_NAMES = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)


# ---------------------------------------------------------------------------
# Step 1: Locate the latest publication page on gov.uk
# ---------------------------------------------------------------------------

def _fetch_latest_deflator_publication_url() -> str:
    """Find the latest HMT GDP deflator publication page URL.

    Scrapes the gov.uk collection page and returns the first matching
    publication link, which gov.uk always lists most-recent-first.
    """
    resp = requests.get(_COLLECTION_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Collect every link that points at a GDP-deflator publication page,
    # preserving page order and removing duplicates.
    candidates: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/government/statistics/gdp-deflators-at-market-prices-and-money-gdp"):
            full = "https://www.gov.uk" + href
            if full not in candidates:
                candidates.append(full)

    if not candidates:
        raise ValueError("No GDP deflator publication links found on collection page.")
    return candidates[0]


# ---------------------------------------------------------------------------
# Step 2: Find the downloadable data file on that publication page
# ---------------------------------------------------------------------------

def _fetch_deflator_xlsx_url(publication_url: str) -> str:
    """Find the xlsx/xls/ods download link from the publication page."""
    resp = requests.get(publication_url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Take the first link ending in a spreadsheet extension — HMT publishes
    # exactly one data file per release alongside supporting PDFs/HTML.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(href.lower().endswith(ext) for ext in (".xlsx", ".xls", ".ods")):
            return "https://www.gov.uk" + href if href.startswith("/") else href

    raise ValueError("No downloadable data file found on publication page.")


# ---------------------------------------------------------------------------
# Step 3: Read the publication's month/year for display purposes
# ---------------------------------------------------------------------------

def _fetch_deflator_publication_month_year(publication_url: str) -> str:
    """Extract the publication month/year from the HMT publication page title.

    Expected title pattern example:
    "GDP deflators at market prices, and money GDP June 2026 (Quarterly
    National Accounts)"
    """
    resp = requests.get(publication_url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Prefer the <title> tag; fall back to the page's <h1> if title is empty.
    title_text = ""
    if soup.title and soup.title.string:
        title_text = soup.title.string.strip()
    if not title_text:
        h1 = soup.find("h1")
        if h1:
            title_text = h1.get_text(" ", strip=True)

    m = re.search(rf"\b({_MONTH_NAMES})\s+(\d{{4}})\b", title_text)
    return f"{m.group(1)} {m.group(2)}" if m else "Unknown publication date"


# ---------------------------------------------------------------------------
# Step 4: Parse the downloaded workbook into a clean Year/Deflator table
# ---------------------------------------------------------------------------

def _parse_calendar_year_series(series: pd.Series) -> pd.Series:
    """Coerce a mixed-type column into whole calendar years (e.g. 2014).

    HMT workbooks mix numeric year cells, floats like ``2014.0``, and
    plain text; financial-year rows (e.g. "2014-15") are intentionally
    excluded so only whole calendar years are used.
    """
    years: list[float] = []
    for v in series:
        if pd.isna(v):
            years.append(np.nan)
            continue
        # Case 1: already an integer-typed year.
        if isinstance(v, (int, np.integer)) and 1900 <= int(v) <= 2100:
            years.append(float(v))
            continue
        # Case 2: a float that represents a whole year (e.g. 2014.0).
        if isinstance(v, (float, np.floating)) and float(v).is_integer() and 1900 <= int(v) <= 2100:
            years.append(float(v))
            continue
        # Case 3: a 4-digit numeric string.
        s = str(v).strip()
        if s.isdigit() and len(s) == 4 and 1900 <= int(s) <= 2100:
            years.append(float(s))
            continue
        # Anything else (blank cells, financial-year ranges, footnotes, etc.)
        # is not a usable calendar year.
        years.append(np.nan)
    return pd.Series(years, index=series.index, dtype="float64")


def _parse_deflator_xlsx(file_bytes: bytes) -> pd.DataFrame:
    """Parse an HMT deflator workbook into a ``Year``/``Deflator`` dataframe.

    Uses whole calendar years only (e.g. 2014), not financial years. HMT
    workbook layouts vary release to release (sheet name, header row, exact
    column wording), so this scans a range of header rows across every sheet
    and keeps the richest usable table found.
    """
    xl = pd.ExcelFile(BytesIO(file_bytes))
    candidates_whole: list[pd.DataFrame] = []

    for sheet_name in xl.sheet_names:
        # Try a range of plausible header-row positions since the real
        # header is preceded by a variable number of title/note rows.
        for header_row in range(4, 13):
            try:
                df = pd.read_excel(xl, sheet_name=sheet_name, header=header_row)
            except Exception:
                continue
            if df.empty:
                continue

            cols = list(df.columns)
            # Deflator columns are always labelled "<base year> = 100".
            deflator_cols = [c for c in cols if "= 100" in str(c)]
            if not deflator_cols:
                continue
            # Prefer columns explicitly tied to a 4-digit base year over any
            # other "= 100" column (e.g. financial-year variants).
            calendar_deflator_cols = [
                c for c in deflator_cols if re.search(r"\b\d{4}\s*=\s*100\b", str(c))
            ]

            for year_col in df.columns:
                # Only treat columns literally named "Calendar year" as the
                # year axis; financial-year columns are skipped entirely.
                if "calendar year" not in str(year_col).strip().lower():
                    continue

                # Pick whichever deflator column sits closest to this year
                # column in the sheet, since layouts place related series
                # side-by-side.
                year_idx = cols.index(year_col)
                pool = calendar_deflator_cols if calendar_deflator_cols else deflator_cols
                deflator_col_name = min(pool, key=lambda c: abs(cols.index(c) - year_idx))
                defl = pd.to_numeric(df[deflator_col_name], errors="coerce")

                whole_years = _parse_calendar_year_series(df[year_col])
                whole = pd.DataFrame({"Year": whole_years, "Deflator": defl}).dropna()
                if whole.empty:
                    continue
                whole["Year"] = whole["Year"].astype(int)
                whole = whole[(whole["Year"] >= 1900) & (whole["Year"] <= 2100)]
                if not whole.empty:
                    whole = whole.drop_duplicates(subset=["Year"], keep="first").sort_values("Year")
                    candidates_whole.append(whole)

    if candidates_whole:
        # Different (sheet, header_row) combinations can each yield a valid
        # but partial table; the one covering the most years is the best
        # representation of the full published series.
        return max(candidates_whole, key=len).reset_index(drop=True)

    raise ValueError(
        "Could not find a usable calendar-year GDP deflator table in the "
        "latest gov.uk GDP deflator workbook."
    )


# ---------------------------------------------------------------------------
# Step 5: Cached top-level loader (one network round-trip per 24h per app)
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Loading latest HMT GDP deflators...", ttl=86400)
def cached_load_hmt_deflators_with_meta() -> tuple[pd.DataFrame, str, str]:
    """Load latest HMT GDP deflators from gov.uk, cached for 24 hours.

    Returns a tuple of (deflators dataframe, publication "Month Year"
    string, publication page URL) so the sidebar can cite its source.
    """
    publication_url = _fetch_latest_deflator_publication_url()
    publication_month_year = _fetch_deflator_publication_month_year(publication_url)
    xlsx_url = _fetch_deflator_xlsx_url(publication_url)
    resp = requests.get(xlsx_url, timeout=30)
    resp.raise_for_status()
    deflators_df = _parse_deflator_xlsx(resp.content)
    return deflators_df, publication_month_year, publication_url


# ---------------------------------------------------------------------------
# Step 6: Convert between two price-base years
# ---------------------------------------------------------------------------

def get_price_base_factor(deflators_df: pd.DataFrame, from_year: int, to_year: int) -> float:
    """Return the multiplier that rebases costs from ``from_year`` to ``to_year`` prices.

    Multiplying a cost expressed in ``from_year`` prices by this factor
    yields the equivalent cost expressed in ``to_year`` prices, following
    the standard GDP-deflator rebasing formula: factor = deflator(to_year) /
    deflator(from_year).
    """
    lookup = dict(zip(deflators_df["Year"].astype(int), deflators_df["Deflator"].astype(float)))
    if from_year not in lookup:
        raise ValueError(f"{from_year} not found in GDP deflator data")
    if to_year not in lookup:
        raise ValueError(f"{to_year} not found in GDP deflator data")
    return lookup[to_year] / lookup[from_year]
