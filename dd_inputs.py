from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

from cost_allocation.constants import MODEL_PERIODS
from cost_allocation.cost_extraction import compute_totcapex, _extract_coeff_from_base_data_long


# TIMES Interpolation/Extrapolation option codes (year=0 records)
# Reference: "DD creation.txt" §5.6 (from TIMES documentation Parts I-IV)
#
#   Code  Action
#   ----  ------
#    0    Default = full interpolation + constant forward & backward extrapolation
#         (equivalent to 3; this is the default for all params not in §5.7)
#    <0   NO interpolation or extrapolation
#    1    Interpolation only, NO extrapolation
#    2    Interpolation + EPS (zero) outside the data window
#    3    Forced interpolation + forward AND backward constant extrapolation
#    4    Interpolation + backward constant extrapolation only
#    5    Interpolation + forward constant extrapolation only
#   10+   Migrated variants (not used by our TARGET_ATTRS)
#
_IE_INTERP = {0, 1, 2, 3, 4, 5}          # All non-negative codes interpolate
_IE_FORWARD_EXTRAP = {0, 3, 5}           # Codes enabling constant forward extrapolation
_IE_BACKWARD_EXTRAP = {0, 3, 4}          # Codes enabling constant backward extrapolation
_IE_EPS_OUTSIDE = {2}                    # Code 2: fill with 0.0 (EPS) outside window

# Attributes that have no year dimension → excluded from default I/E
_YEAR_INDEPENDENT_ATTRS = {"NCAP_START", "PRC_CAPACT"}


_NUM_RE = re.compile(r"^(.*?)\s+([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s*$")
_TOKEN_RE = re.compile(r"'([^']*)'|([A-Za-z0-9_\-]+)")

TARGET_ATTRS = {
    "NCAP_START",
    "NCAP_ELIFE",
    "NCAP_TLIFE",
    "NCAP_ILED",
    "NCAP_DRATE",
    "NCAP_COST",
    "NCAP_FOM",
    "ACT_COST",
    "PRC_CAPACT",
    "G_DRATE",
    "G_DYEAR",
    "G_RFRIR",
}


def parse_run_include_order(run_path: Path) -> list[str]:
    """Read $BATINCLUDE order from a .run file."""
    if not run_path.exists():
        return []

    include_re = re.compile(r"^\$BATINCLUDE\s+([^\s]+)", re.IGNORECASE)
    out: list[str] = []
    for raw in run_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = include_re.match(line)
        if m:
            out.append(Path(m.group(1)).name.lower())
    return out


def _parse_numeric_entry(line: str) -> tuple[list[str], float] | None:
    m = _NUM_RE.match(line.strip().rstrip(","))
    if not m:
        return None
    key_side = m.group(1).strip()
    val = float(m.group(2))
    tokens = [a or b for a, b in _TOKEN_RE.findall(key_side)]
    if not tokens:
        return None
    return tokens, val


def _looks_like_year(tok: str) -> bool:
    t = str(tok).strip()
    return t.isdigit() and len(t) == 4 and int(t) >= 1900


def _normalize_row(attr: str, tokens: list[str], val: float, scenario: str, source_file: str) -> dict:
    attr_u = attr.upper()
    region = tokens[0] if len(tokens) >= 1 else "-"
    process = "-"
    year = "-"
    currency = "-"

    if attr_u in {"NCAP_DRATE", "NCAP_ELIFE", "NCAP_TLIFE", "NCAP_ILED"}:
        year = tokens[1] if len(tokens) >= 2 else "-"
        process = tokens[2] if len(tokens) >= 3 else "-"
    elif attr_u in {"NCAP_COST", "NCAP_FOM", "ACT_COST"}:
        year = tokens[1] if len(tokens) >= 2 else "-"
        process = tokens[2] if len(tokens) >= 3 else "-"
        currency = tokens[3] if len(tokens) >= 4 else "-"
    elif attr_u == "NCAP_START":
        # Typical shape: region, process
        if len(tokens) >= 2 and not _looks_like_year(tokens[1]):
            process = tokens[1]
        elif len(tokens) >= 3:
            process = tokens[2]
        year = "-"
    elif attr_u == "PRC_CAPACT":
        # Typical shape: region, process (no year dimension)
        if len(tokens) >= 2 and not _looks_like_year(tokens[1]):
            process = tokens[1]
        elif len(tokens) >= 3:
            process = tokens[2]
        year = "-"
    elif attr_u in {"G_DRATE", "G_DYEAR", "G_RFRIR"}:
        process = "-"
        # G_DRATE format: region.year.currency (e.g. 'UK'.2010.'GBP')
        # or just year (e.g. 2010).  Find the first year-like token.
        year = "-"
        for tok in tokens:
            if _looks_like_year(tok):
                year = tok
                break
    else:
        year = tokens[1] if len(tokens) >= 2 else "-"
        process = tokens[2] if len(tokens) >= 3 else "-"

    return {
        "attribute": attr_u,
        "region": region,
        "scenario": scenario,
        "process": process,
        "year": year,
        "currency": currency,
        "val_field": float(val),
        "source_file": source_file,
    }


log = logging.getLogger(__name__)


def _extract_rows_from_dd(dd_path: Path, target_attrs: set[str]) -> list[dict]:
    rows: list[dict] = []
    scenario = dd_path.stem.upper()

    expect_attr = False
    in_block = False
    current_attr: str | None = None

    with dd_path.open("r", encoding="utf-8", errors="ignore") as fh:
      for raw in fh:
        line = raw.strip()

        if line.startswith("*"):
            continue

        if not in_block:
            if line.upper().startswith("PARAMETER"):
                expect_attr = True
                current_attr = None
                continue
            if expect_attr and line:
                current_attr = line.split()[0].upper()
                in_block = "/" in line
                expect_attr = False
                continue
            continue

        if line.startswith("/;"):
            in_block = False
            current_attr = None
            continue

        if not current_attr or current_attr not in target_attrs:
            continue

        parsed = _parse_numeric_entry(line)
        # SysSettings defines G_DYEAR as a scalar parameter, for example:
        #
        #     G_DYEAR ' '/
        #     2010
        #     /;
        #
        # Unlike indexed DD entries, its value line has no key tokens, so it
        # cannot be handled by _parse_numeric_entry.
        if parsed is None and current_attr == "G_DYEAR":
            try:
                scalar_value = float(line.rstrip(","))
            except ValueError:
                pass
            else:
                rows.append(
                    _normalize_row(current_attr, [], scalar_value, scenario, dd_path.name)
                )
                continue
        if not parsed:
            continue
        toks, val = parsed
        rows.append(_normalize_row(current_attr, toks, val, scenario, dd_path.name))

      return rows


def _parse_ordered_dd_rows(dd_dir: str | Path, dd_file_list: list[str]) -> pd.DataFrame:
    """Parse every listed DD file and tag rows with their include-order rank.

    Shared by :func:`load_effective_dd_inputs` and
    :func:`extract_all_dd_attributes` so both see identical parsing,
    precedence and ordering.
    """
    dd_path = Path(dd_dir)
    if not dd_path.exists():
        return pd.DataFrame()

    file_order = {name.lower(): i + 1 for i, name in enumerate(dd_file_list)}
    rows: list[dict] = []
    for name in dd_file_list:
        f = dd_path / f"{name}.dd"
        if f.exists():
            rows.extend(_extract_rows_from_dd(f, TARGET_ATTRS))
        else:
            log.warning("DD file not found: %s", f)
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    dd_stems = df["source_file"].str.lower().str.replace(r"\.dd$", "", regex=True)
    df["_dedup_order"] = dd_stems.map(file_order)
    return df


def _apply_times_interpolation(
    eff: pd.DataFrame,
    ie_options: pd.DataFrame,
) -> pd.DataFrame:
    """Apply TIMES inter/extrapolation per DD creation.txt §5.

    Stage 2 of TIMES data processing: after all DD files are merged
    (Stage 1, handled by ``load_effective_dd_inputs``), the I/E routine
    runs over the **union** of all surviving data points for each
    (attribute, process) series.

    Default behaviour (no explicit year=0 IE code) is option 0 which is
    identical to option 3: full interpolation + constant forward and
    backward extrapolation.  All TARGET_ATTRS use this default (none
    appear in §5.7's exception list).

    Parameters
    ----------
    eff : DataFrame
        Effective data rows (year=0 already removed, deduped).
    ie_options : DataFrame
        Year=0 records carrying IE option values (already deduped so
        last-file-wins per merge Rule 5).

    Returns
    -------
    DataFrame with interpolated/extrapolated rows appended.
    """
    # Build lookup: (attribute, process) → IE code from explicit year=0 records
    ie_lookup: dict[tuple[str, str], int] = {}
    if not ie_options.empty:
        for _, row in ie_options.iterrows():
            key = (row["attribute"], row["process"])
            try:
                ie_lookup[key] = int(float(row["val_field"]))
            except (ValueError, TypeError):
                continue

    # Apply DEFAULT I/E (option 0 = same as 3) for every (attr, process)
    # that does NOT have an explicit IE code.  Year-independent attributes
    # (NCAP_START, PRC_CAPACT) are excluded.
    yr_num_col = pd.to_numeric(eff["year"], errors="coerce")
    time_series_mask = yr_num_col.notna() & ~eff["attribute"].isin(_YEAR_INDEPENDENT_ATTRS)
    for (attr, proc) in eff.loc[time_series_mask, ["attribute", "process"]].drop_duplicates().values:
        if (attr, proc) not in ie_lookup:
            ie_lookup[(attr, proc)] = 0  # default = option 0 (full I/E)

    if not ie_lookup:
        return eff

    # Group the numeric-year rows per series once, instead of re-filtering the
    # whole frame for every (attribute, process). Row order within each group
    # matches the old boolean-mask selection.
    series_rows = eff.loc[yr_num_col.notna()].assign(_yr_num=yr_num_col.loc[yr_num_col.notna()])
    series_groups = dict(tuple(series_rows.groupby(["attribute", "process"], sort=False)))

    new_rows: list[dict] = []
    periods = list(MODEL_PERIODS)

    for (attr, proc), ie_code in ie_lookup.items():
        # Negative codes → no I/E at all
        if ie_code < 0:
            continue
        if ie_code not in _IE_INTERP:
            continue

        sub = series_groups.get((attr, proc))
        if sub is None or sub.empty:
            continue

        sub = sub.sort_values("_yr_num")
        known_years = sub["_yr_num"].values
        known_vals = sub["val_field"].values

        if len(known_years) < 1:
            continue

        min_yr = int(known_years[0])
        max_yr = int(known_years[-1])

        # Determine target years to interpolate onto
        target_years = [p for p in periods if p not in known_years]

        for yr in target_years:
            if yr < min_yr:
                # Backward extrapolation
                if ie_code in _IE_BACKWARD_EXTRAP:
                    val = float(known_vals[0])  # constant backward
                elif ie_code in _IE_EPS_OUTSIDE:
                    val = 0.0  # EPS = effectively zero
                else:
                    continue  # code 1 or 5 → no backward extrap
            elif yr > max_yr:
                # Forward extrapolation
                if ie_code in _IE_FORWARD_EXTRAP:
                    val = float(known_vals[-1])  # constant forward
                elif ie_code in _IE_EPS_OUTSIDE:
                    val = 0.0  # EPS = effectively zero
                else:
                    continue  # code 1 or 4 → no forward extrap
            else:
                # Interpolation between known years (linear)
                val = float(np.interp(yr, known_years, known_vals))

            # Inherit metadata from the nearest source row
            template = sub.iloc[-1] if yr >= max_yr else sub.iloc[0]
            new_rows.append({
                "attribute": attr,
                "region": template.get("region", "-"),
                "scenario": template.get("scenario", ""),
                "process": proc,
                "year": str(yr),
                "currency": template.get("currency", "-"),
                "val_field": val,
                "source_file": template.get("source_file", ""),
                "_dedup_order": template.get("_dedup_order", 0),
            })

    if not new_rows:
        return eff

    interp_df = pd.DataFrame(new_rows)
    combined = pd.concat([eff, interp_df], ignore_index=True)

    # Re-dedup: interpolated rows should NOT override explicit data
    key_cols = ["attribute", "region", "process", "year", "currency"]
    combined = combined.drop_duplicates(subset=key_cols, keep="first")

    return combined


def load_effective_dd_inputs(
    dd_dir: str | Path,
    dd_file_list: list[str],
) -> pd.DataFrame:
    """Load DD inputs and collapse to effective final values.

    Parameters
    ----------
    dd_dir : str or Path
        Directory containing .dd files.
    dd_file_list : list of str
        Explicit list of DD file stems (no ``.dd`` extension) to include,
        in precedence order (from the .RUN file's $BATINCLUDE lines).
        Later entries win on dedup.
    """
    df = _parse_ordered_dd_rows(dd_dir, dd_file_list)
    if df.empty:
        return pd.DataFrame(columns=["attribute", "scenario", "process", "year", "val_field"])

    df = df.dropna(subset=["_dedup_order"]).copy()
    df["_dedup_order"] = df["_dedup_order"].astype(int)

    df = df.sort_values(["_dedup_order", "source_file"]).reset_index(drop=True)

    # --- Separate year=0 IE option codes from real data ---
    # In TIMES, year=0 records carry interpolation/extrapolation option codes
    # (e.g. IE=3 means "full interp+extrap"), NOT actual values.
    year_num = pd.to_numeric(df["year"], errors="coerce")
    ie_mask = (year_num == 0) & df["year"].notna()
    ie_options_raw = df[ie_mask].copy()
    df = df[~ie_mask].copy()

    if df.empty:
        return pd.DataFrame(columns=["attribute", "scenario", "process", "year", "val_field"])

    # Highest-order value wins per key (merge Rule 5: same index = last wins;
    # Rule 6: different year index entries coexist as additions).
    key_cols = ["attribute", "region", "process", "year", "currency"]
    eff = df.drop_duplicates(subset=key_cols, keep="last").copy()

    # Dedup IE option codes the same way (last-wins per merge Rule 5).
    # IE year=0 records share the same index across files, so the later
    # file's IE code overrides the earlier file's for the same (attr, process).
    ie_key_cols = ["attribute", "region", "process", "year", "currency"]
    ie_options = ie_options_raw.drop_duplicates(subset=ie_key_cols, keep="last").copy()

    # --- Apply TIMES interpolation/extrapolation (Stage 2) ---
    # Runs over the union of all surviving data points.  Default = option 0
    # (= option 3: full interp + both extrap) for all TARGET_ATTRS.
    eff = _apply_times_interpolation(eff, ie_options)

    # Emit a BASE-tagged long table so existing loaders can consume it directly.
    eff["scenario"] = "BASE"
    out_cols = ["attribute", "scenario", "process", "year", "val_field", "source_file"]
    out = eff[[c for c in out_cols if c in eff.columns]].copy()
    out["process"] = out["process"].astype(str).str.strip()
    out = out[(out["attribute"] != "") & (out["process"] != "")]
    return out


def build_input_costs_from_dd_effective(
    dd_effective_df: pd.DataFrame,
    vd_df: pd.DataFrame,
    scenario: str,
) -> dict[str, pd.DataFrame]:
    """Build app input_costs dict from effective DD long-format values."""
    model_years = None
    if not vd_df.empty and "Period" in vd_df.columns:
        periods = pd.to_numeric(vd_df["Period"], errors="coerce").dropna().unique()
        periods = [int(p) for p in periods if p >= 2000]
        if periods:
            model_years = sorted(periods)

    capex_df = _extract_coeff_from_base_data_long(dd_effective_df, "NCAP_COST", {}, model_years)
    fom_df = _extract_coeff_from_base_data_long(dd_effective_df, "NCAP_FOM", {}, model_years)
    act_df = _extract_coeff_from_base_data_long(dd_effective_df, "ACT_COST", {}, model_years)

    for tbl in [capex_df, fom_df, act_df]:
        if not tbl.empty:
            for c in tbl.columns:
                tbl[c] = pd.to_numeric(tbl[c], errors="coerce")

    totcapex_df = compute_totcapex(capex_df, vd_df, scenario)

    return {
        "capex": capex_df,
        "fom": fom_df,
        "act_cost": act_df,
        "totcapex": totcapex_df,
    }


def extract_all_dd_attributes(
    dd_dir: str | Path,
    dd_file_list: list[str],
) -> pd.DataFrame:
    """Extract ALL target attributes from every DD file with full provenance.

    Unlike ``load_effective_dd_inputs`` which collapses to effective values,
    this returns *every* row parsed from every DD file so the caller can
    see which files define a given attribute for a given process, and which
    value ultimately wins after dedup.

    Returns a DataFrame with columns:
        attribute, region, scenario, process, year, currency, val_field,
        source_file, _dedup_order, is_ie_option, is_effective
    """
    df = _parse_ordered_dd_rows(dd_dir, dd_file_list)
    if df.empty:
        return pd.DataFrame()

    df = df.sort_values(["_dedup_order", "source_file"]).reset_index(drop=True)

    # Tag year=0 rows as IE option codes (not real data).
    year_num = pd.to_numeric(df["year"], errors="coerce")
    df["is_ie_option"] = (year_num == 0) & df["year"].notna()

    # Mark which rows are effective (win the dedup).
    # Only non-IE rows participate in dedup.
    key_cols = ["attribute", "region", "process", "year", "currency"]
    data_df = df[~df["is_ie_option"]]
    eff_idx = data_df.dropna(subset=["_dedup_order"]).drop_duplicates(
        subset=key_cols, keep="last"
    ).index
    df["is_effective"] = df.index.isin(eff_idx)

    return df
