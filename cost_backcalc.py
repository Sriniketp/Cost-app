"""
Cost Back-Calculation and TIMES Input Recovery
===============================================

Reverse-engineer TIMES input parameters (NCAP_COST, ACT_COST, NCAP_FOM) from
reported cost attributes (Cost_Inv, Cost_Act, Cost_Fom).

Methods:
- Load reference data (NCAP_TLIFE, NCAP_ELIFE, NCAP_DRATE, g_drate)
- Calculate Capital Recovery Factor (CRF)
- Back-calculate NCAP_COST from Cost_Inv
- Format cost breakdown with step-by-step calculations
"""

from __future__ import annotations

import functools
import math
import pandas as pd
from pathlib import Path


# ---------------------------------------------------------------------------
# CRF Calculation
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=512)
def calculate_crf(discount_rate: float, economic_life: int, method: str = "mid_year") -> float:
    """
    Calculate Capital Recovery Factor (CRF).

    CRF annualizes investment cost over economic life.

    Parameters
    ----------
    discount_rate : float
        Discount rate as decimal (e.g., 0.08 for 8%).
    economic_life : int
        Economic life in years (NCAP_ELIFE).
    method : str
        "mid_year" (default, matches this TIMES model which has $SET MID_YEAR YES),
        "beginning_of_year", or "end_of_year".

    Returns
    -------
    float
        CRF value.

    Notes
    -----
    Beginning-of-year (TIMES standard): CRF = (1 - (1+r)^(-1)) / (1 - (1+r)^(-L))
    End-of-year: CRF = r / (1 - (1+r)^(-L))
    Mid-year ($SET MIDYEAR YES): CRF_midyear = CRF_standard × (1+r)^0.5
      per TIMES documentation (Investment Case Classification §10).
      Note: this is NOT the arithmetic mean of beginning- and end-of-year CRFs.
    """
    if economic_life <= 0:
        return 0.0

    r = discount_rate
    L = economic_life

    if abs(r) < 1e-10:  # Handle zero discount rate
        return 1.0 / L

    denom = 1.0 - (1.0 + r) ** (-L)

    if method == "beginning_of_year":
        return (1.0 - (1.0 + r) ** (-1)) / denom
    elif method == "end_of_year":
        return r / denom
    elif method == "mid_year":
        # TIMES documentation (Investment Case Classification §10):
        # CRF_midyear = CRF_standard × (1+r)^0.5
        # where CRF_standard is the beginning-of-year formula.
        crf_beg = (1.0 - (1.0 + r) ** (-1)) / denom
        return crf_beg * (1.0 + r) ** 0.5
    else:
        raise ValueError(f"Unknown CRF method: {method}")


# ---------------------------------------------------------------------------
# OBLONG period boundaries and discounted pay fraction
# ---------------------------------------------------------------------------

def _oblong_B(milestone: int, D: int) -> int:
    """OBLONG period start year: B(t) = M(t) - FLOOR(D(t)/2)."""
    return milestone - D // 2


def _discounted_pay_frac(k: int, D: int, g: float) -> float:
    """Discounted pay fraction for *k* active years in a period of *D* years.

    Under ``$SET OBLONG YES``, TIMES prorates partial-period payments
    using the ratio of discounted annuity factors:

        pay_frac = AF(g, k) / AF(g, D)
                 = (1 - (1+g)^(-k)) / (1 - (1+g)^(-D))

    This equals 1.0 when k >= D and 0.0 when k <= 0.
    """
    if k <= 0:
        return 0.0
    if k >= D:
        return 1.0
    if abs(g) < 1e-10:
        return k / D  # zero rate → linear proration
    return (1.0 - (1.0 + g) ** (-k)) / (1.0 - (1.0 + g) ** (-D))


# ---------------------------------------------------------------------------
# Investment Case Classification (Guide §4.4, R classify_investment_case)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=512)
def classify_investment_case(
    iled: float | None,
    tlife: float | None,
    period_duration: float,
) -> str:
    """
    Classify the TIMES investment tracking case for a (process, period).

    TIMES handles four cases depending on whether the lead-time ILED is
    "small" and whether the technical life TLIFE is shorter than the period
    duration D(t) (requiring repeated investment).

    Parameters
    ----------
    iled : float or None
        NCAP_ILED value (negative means construction starts early).
        None or 0 means no significant lead-time.
    tlife : float or None
        Technical life in years.  If None, defaults to G_TLIFE = 10
        (the TIMES default when NCAP_TLIFE is not specified).
    period_duration : float
        D(t) — length of the period in years.

    Returns
    -------
    str
        One of ``"case_1a"``, ``"case_1b"``, ``"case_2a"``, ``"case_2b"``,
        or ``"unknown"``.

    Notes
    -----
    Per the TIMES documentation (Part II §6.2) and the investment case
    classification reference note:

    **Test 1 — Small vs Large project:**
        ILEDMin = min(D(t)/10, TLIFE/10)
        |ILED| ≤ ILEDMin → Case 1 (small)
        |ILED| > ILEDMin → Case 2 (large)

    **Test 2 — Single vs Repeated investment:**
        TLIFE + ILED ≥ D(t) → sub-case (a) — single
        TLIFE + ILED < D(t) → sub-case (b) — repeated

    Note: Both tests use |ILED| (magnitude).  Test 1 compares construction
    duration against the small-project threshold.  Test 2 checks whether
    TLIFE + |ILED| covers the period.  The sign of ILED only affects
    *when* construction starts (negative = earlier), not the duration.

    Default cascade: NCAP_TLIFE → G_TLIFE (10), NCAP_ILED → 0.
    """
    if period_duration <= 0:
        return "unknown"

    # Default TLIFE to G_TLIFE = 10 when not specified
    if tlife is None:
        tlife_f = 10.0
    else:
        tlife_f = float(tlife)
    if tlife_f <= 0:
        return "unknown"

    iled_raw = float(iled) if iled is not None else 0.0

    # Test 1: Small vs Large — uses |ILED| (magnitude of construction time)
    iled_abs = abs(iled_raw)
    iled_min = min(period_duration / 10.0, tlife_f / 10.0)
    is_small_iled = iled_abs == 0 or iled_abs <= iled_min

    # Test 2: Single vs Repeated — uses |ILED| (magnitude)
    # TLIFE + |ILED| ≥ D(t) → single investment (sub-case a)
    # TLIFE + |ILED| < D(t) → repeated investment (sub-case b)
    # Negative ILED only shifts construction earlier; it does not
    # reduce the total project coverage (TLIFE + construction duration).
    has_repetition = (tlife_f + iled_abs) < period_duration

    if is_small_iled and has_repetition:
        return "case_1b"
    elif is_small_iled and not has_repetition:
        return "case_1a"
    elif not is_small_iled and has_repetition:
        return "case_2b"
    elif not is_small_iled and not has_repetition:
        return "case_2a"
    return "unknown"


def _period_start_from_milestone(period_year: int, period_duration: float) -> int:
    """Infer period start year B(t) from milestone M(t) and D(t)."""
    d = max(int(round(period_duration)), 1)
    return int(period_year) - d + 1


def _period_years(period_year: int, period_duration: float) -> list[int]:
    """Return the integer years represented by a period milestone."""
    start = _period_start_from_milestone(period_year, period_duration)
    end = int(period_year)
    return list(range(start, end + 1))


def _investment_year_weights(
    vintage_year: int,
    investment_case: str,
    tlife: float,
    iled: float,
    period_duration: float,
) -> dict[int, float]:
    """Return yearly investment weights for one build decision by TIMES case.

    Output values are fractions of VAR_Ncap invested in each year. For repeated
    cases (1b/2b), the sum of weights can exceed 1 because replacement cycles
    are represented explicitly.
    """
    d_t = max(int(round(period_duration)), 1)
    tlife_i = max(int(round(tlife)), 1)
    iled_i = int(round(iled))
    b_t = _period_start_from_milestone(vintage_year, d_t)

    weights: dict[int, float] = {}

    def _add_uniform(start_year: int, n_years: int, share_per_year: float) -> None:
        for y in range(start_year, start_year + n_years):
            weights[y] = weights.get(y, 0.0) + share_per_year

    if investment_case == "case_1a":
        _add_uniform(b_t, d_t, 1.0 / float(d_t))
        return weights

    if investment_case == "case_1b":
        cycles = max(1, int(math.ceil(float(d_t) / float(tlife_i))))
        first_cycle_start = b_t - int(math.ceil(float(tlife_i) / 2.0))
        for i in range(cycles):
            _add_uniform(first_cycle_start + i * tlife_i, tlife_i, 1.0 / float(tlife_i))
        return weights

    if investment_case == "case_2a":
        # Case 2: construction starts at OBLONG B(t), not M(t).
        # Positive ILED: construction from B(t) to B(t)+ILED-1 → capacity at B(t)+ILED.
        # Negative ILED: construction from B(t)+ILED to B(t)-1 → capacity at B(t).
        b_v = _oblong_B(int(vintage_year), d_t)
        phase_years = max(abs(iled_i), 1)
        phase_start = b_v + (iled_i if iled_i < 0 else 0)
        _add_uniform(phase_start, phase_years, 1.0 / float(phase_years))
        return weights

    if investment_case == "case_2b":
        b_v = _oblong_B(int(vintage_year), d_t)
        phase_years = max(abs(iled_i), 1)
        cycles = max(1, int(math.ceil(max(float(d_t) - float(abs(iled_i)), 1.0) / float(tlife_i))))
        for i in range(cycles):
            cycle_start = b_v + i * tlife_i + (iled_i if iled_i < 0 else 0)
            _add_uniform(cycle_start, phase_years, 1.0 / float(phase_years))
        return weights

    weights[int(vintage_year)] = 1.0
    return weights


def _payment_fraction_for_year(
    vintage_year: int,
    investment_case: str,
    tlife: float,
    iled: float,
    period_duration: float,
    elife: float,
    year: int,
) -> float:
    """Fraction of full-vintage annuity payable in a given year."""
    elife_i = int(round(elife))
    if elife_i <= 0:
        return 0.0

    weights = _investment_year_weights(
        vintage_year=vintage_year,
        investment_case=investment_case,
        tlife=tlife,
        iled=iled,
        period_duration=period_duration,
    )

    frac = 0.0
    for invest_year, share in weights.items():
        pay_end = invest_year + elife_i - 1
        if invest_year <= int(year) <= pay_end:
            frac += float(share)
    return frac


def _payment_fraction_for_period(
    vintage_year: int,
    investment_case: str,
    tlife: float,
    iled: float,
    period_duration: float,
    elife: float,
    period_year: int,
) -> float:
    """Average annual payment fraction across years represented by a period."""
    years = _period_years(period_year, period_duration)
    if not years:
        return 0.0
    total = 0.0
    for y in years:
        total += _payment_fraction_for_year(
            vintage_year=vintage_year,
            investment_case=investment_case,
            tlife=tlife,
            iled=iled,
            period_duration=period_duration,
            elife=elife,
            year=y,
        )
    return total / float(len(years))


def _discount_factor(year: int, ref_year: int, social_rate: float) -> float:
    """Compute social discount factor to reference year."""
    r = float(social_rate)
    if abs(r) < 1e-12:
        return 1.0
    return (1.0 + r) ** (int(ref_year) - int(year))


def _estimate_salvage_proxy_for_vintage(
    vintage_year: int,
    investment_case: str,
    tlife: float,
    iled: float,
    period_duration: float,
    ncap_cost: float,
    var_ncap: float,
    eoh_year: int,
) -> float:
    """Estimate salvage credit proxy from residual technical life beyond EOH.

    This is an approximation for diagnostics and not a full TIMES salvage
    reproduction. Negative value denotes a credit.
    """
    tlife_i = max(int(round(tlife)), 1)
    iled_i = int(round(iled))
    weights = _investment_year_weights(
        vintage_year=vintage_year,
        investment_case=investment_case,
        tlife=float(tlife_i),
        iled=float(iled_i),
        period_duration=period_duration,
    )

    salvage = 0.0
    for invest_year, share in weights.items():
        op_start = int(invest_year) + abs(iled_i)
        op_end = op_start + tlife_i - 1
        if op_end <= int(eoh_year):
            continue
        remaining = float(op_end - int(eoh_year))
        salvage_frac = min(max(remaining / float(tlife_i), 0.0), 1.0)
        salvage += -salvage_frac * float(share) * float(ncap_cost) * float(var_ncap)
    return salvage


def back_calc_ncap_cost(cost_inv: float, crf: float) -> float:
    """
    Back-calculate NCAP_COST from annualized Cost_Inv.

    NCAP_COST = Cost_Inv / CRF

    .. warning::
        This is only valid for single-vintage processes where Cost_Inv in the
        VD represents payments from exactly one build decision.  For multi-vintage
        processes, Cost_Inv aggregates annuities from all active vintages, making
        the result unreliable.  Prefer totcapex / VAR_Ncap for general use.

    Parameters
    ----------
    cost_inv : float
        Annualized investment cost (£M/year).
    crf : float
        Capital Recovery Factor.

    Returns
    -------
    float
        NCAP_COST (implied lump-sum investment cost).
    """
    if crf <= 0:
        return 0.0
    return cost_inv / crf


def compute_vintage_annuity(
    ncap_cost: float,
    var_ncap: float,
    drate: float,
    gdrate: float,
    elife: int,
    tlife: int,
    inv_case: str = "case_1a",
    iled: float = 0.0,
) -> float | None:
    """
    Compute the full-vintage annualized Cost_Inv for one build decision.

    Single authoritative formula for all investment cases::

        annuity = NCAP_COST × VAR_Ncap × CRF(drate, ELIFE) × relevel × idc_factor

    where:

    - ``CRF(drate, ELIFE)`` — hurdle-rate annuity over economic life (mid-year).
    - ``relevel = CRF(gdrate, TLIFE) / CRF(gdrate, ELIFE)`` when ELIFE ≠ TLIFE,
      otherwise 1.0.  Re-levelizes from the economic-life annuity to the
      technical-life basis at the social discount rate, matching RPT_OPT OBJ 2.
    - ``idc_factor = (1 + gdrate)^(|ILED|/2)`` for Case 2 (large projects with
      significant construction lead time), otherwise 1.0.  NCAP_COST for Case 2
      is the overnight cost; IDC is computed endogenously at the general (social)
      discount rate (Part II §6.2).  Case 1 NCAP_COST already includes IDC.

    Investment case classification only affects *timing* (OBLONG pay fractions),
    not this annuity value — **except** for the IDC factor which applies to
    Case 2a/2b only.

    Parameters
    ----------
    ncap_cost : float
        Capital cost per unit (£M/unit).
    var_ncap : float
        New capacity built (units).
    drate : float
        Hurdle (technology-specific) discount rate as decimal.
    gdrate : float
        Social (general) discount rate as decimal.
    elife : int
        Economic life in years (NCAP_ELIFE).
    tlife : int
        Technical life in years (NCAP_TLIFE).
    inv_case : str
        Investment case from ``classify_investment_case()``.
    iled : float
        Lead time in years (NCAP_ILED); sign is ignored (magnitude used).

    Returns
    -------
    float or None
        Annualized Cost_Inv in same units as ncap_cost × var_ncap.
        Returns None when inputs are invalid or CRF cannot be computed.
    """
    if ncap_cost is None or var_ncap <= 0 or elife <= 0 or tlife <= 0:
        return None
    try:
        crf_hurdle = calculate_crf(float(drate), int(elife))
        if crf_hurdle <= 0:
            return None

        # Re-levelization: CRF(gdrate, TLIFE) / CRF(gdrate, ELIFE).
        # Adjusts the economic-life annuity to the technical-life basis at
        # the social rate.  When ELIFE == TLIFE, relevel = 1.0 exactly.
        if int(elife) != int(tlife):
            crf_g_elife = calculate_crf(float(gdrate), int(elife))
            crf_g_tlife = calculate_crf(float(gdrate), int(tlife))
            relevel = (crf_g_tlife / crf_g_elife) if crf_g_elife > 1e-15 else 1.0
        else:
            relevel = 1.0

        # IDC factor for Case 2 processes.
        # NCAP_COST is the overnight cost; TIMES computes IDC endogenously
        # by discounting construction-phase outlays at the general (social)
        # discount rate, not the hurdle rate (Part II §6.2, Case 2a/2b).
        # Mid-year convention: average outlay at |ILED|/2 years.
        iled_abs = abs(float(iled)) if iled is not None else 0.0
        if inv_case in ("case_2a", "case_2b") and iled_abs > 0:
            idc_factor = (1.0 + float(gdrate)) ** (iled_abs / 2.0)
        else:
            idc_factor = 1.0

        return float(ncap_cost) * float(var_ncap) * crf_hurdle * relevel * idc_factor
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Reference Data Loader
# ---------------------------------------------------------------------------

class ReferenceData:
    """
    Load and manage reference data (NCAP_TLIFE, NCAP_ELIFE, NCAP_DRATE, g_drate).

    Data source: the run's effective DD table, either in memory
    (:meth:`from_frame`, used by the dashboard and pipeline) or a CSV export.

    Example
    -------
    >>> ref = ReferenceData.from_frame(load_effective_dd_inputs(dd_dir, dd_list))
    >>> tlife = ref.get("NCAP_TLIFE", "RESDHP01", 2030)  # Returns 20 (years)
    >>> drate = ref.get("NCAP_DRATE", "RESDHP01")  # Returns 0.08 (8%)
    """

    def __init__(self, path: str | Path, scenario_order: dict[str, int] | None = None) -> None:
        """Initialize and load reference data.

        Parameters
        ----------
        path : str or Path
            Path to DD effective export CSV.
        scenario_order : dict, optional
            Scenario precedence mapping ``{scenario_lower: order_int}`` from
            ``run_selector.dd_list_to_scenario_order()``.  When provided,
            multi-scenario entries
            are collapsed so that only the highest-order value survives per
            (attribute, process, year).  If *None* (default), all entries are
            kept and ``get()`` falls back across scenarios in arbitrary order.
        """
        self._reset(scenario_order)
        self.path: Path | None = Path(path)
        self.source_identity: tuple = ("file", str(self.path))
        self.load()

    @classmethod
    def from_frame(cls, frame: pd.DataFrame) -> ReferenceData:
        """Build reference data from an in-memory effective DD table.

        *frame* is the output of ``dd_inputs.load_effective_dd_inputs``, which
        is already precedence-resolved and tagged ``BASE``, so no scenario
        order is applied. Values are read exactly as a CSV round trip of the
        same frame would read them (missing or empty cells become ``"nan"``).
        """
        ref = cls.__new__(cls)
        ref._reset(None)
        ref.path = None
        ref.source_identity = (
            "frame",
            frame.shape,
            tuple(str(column) for column in frame.columns),
            int(pd.util.hash_pandas_object(frame, index=False).sum()) if not frame.empty else 0,
        )
        if "attribute" in frame.columns:
            ref._ingest_rows(frame.astype(object).where(frame.notna() & frame.ne(""), None))
        ref._rebuild_index()
        return ref

    def _reset(self, scenario_order: dict[str, int] | None) -> None:
        self._data: dict[str, dict] = {}
        self._source: dict[str, dict] = {}  # attr -> {key -> source_file}
        self._scenario_order = scenario_order or {}
        self._profile_cache: dict[tuple, dict] = {}
        self._by_process: dict[str, dict[str, list[tuple[tuple, float]]]] = {}
        self._src_by_process: dict[str, dict[str, list[tuple[tuple, str]]]] = {}

    def load(self) -> None:
        """Load CSV file and populate internal indexes."""
        self._load_csv()
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        """Index entries per (attribute, process), preserving source-row order.

        ``get``/``get_source`` fallbacks return the first match in insertion
        order; iterating a process's own entries in that same order gives the
        identical answer without scanning every entry for the attribute.
        """
        self._by_process = {}
        for attr, attr_data in self._data.items():
            index: dict[str, list[tuple[tuple, float]]] = {}
            for key, val in attr_data.items():
                index.setdefault(key[1], []).append((key, val))
            self._by_process[attr] = index
        self._src_by_process = {}
        for attr, src_map in self._source.items():
            src_index: dict[str, list[tuple[tuple, str]]] = {}
            for key, src in src_map.items():
                src_index.setdefault(key[1], []).append((key, src))
            self._src_by_process[attr] = src_index

    def _load_csv(self) -> None:
        """Load CSV file and populate ``_data``/``_source``."""
        if not self.path.exists():
            # No reference data; use empty dict
            return

        try:
            df = pd.read_csv(self.path, dtype=str, low_memory=False)

            # Expected columns: attribute, scenario, process, year, val_field
            if "attribute" not in df.columns:
                return

            self._ingest_rows(df)

        except Exception as e:
            print(f"Warning: Could not load reference data: {e}")

        # Apply scenario precedence: for each (attribute, process, year),
        # keep only the value from the highest-order scenario in the run's
        # .RUN DD list.
        if self._scenario_order:
            observed = {
                scenario.lower()
                for attr_data in self._data.values()
                for scenario, _process, _year in attr_data
            }
            if observed and not observed & set(self._scenario_order):
                raise ValueError(
                    f"Reference data scenario tag(s) {sorted(observed)!r} do not match any "
                    f"entry in the supplied scenario precedence order "
                    f"{sorted(self._scenario_order)!r}; refusing to silently drop every "
                    "DD-derived parameter."
                )
            self._apply_scenario_precedence()

    def _ingest_rows(self, df: pd.DataFrame) -> None:
        """Populate ``_data``/``_source`` from effective-DD rows, in row order.

        Cells are read as text, as ``read_csv(dtype=str)`` would give them;
        a missing cell (NaN or None) reads as ``"nan"``.
        """
        def column(name: str, default: str) -> list:
            if name in df.columns:
                return ["nan" if value is None else value for value in df[name].tolist()]
            return [default] * len(df)

        has_source = "source_file" in df.columns
        sources = column("source_file", "")
        for attr, scenario, process, year, val, src in zip(
            column("attribute", ""),
            column("scenario", "BASE"),
            column("process", ""),
            column("year", ""),
            column("val_field", ""),
            sources,
        ):
            attr = str(attr).strip()
            scenario = str(scenario).strip()
            process = str(process).strip()
            year = str(year).strip()
            val = str(val).strip()

            if not process or not attr or not val:
                continue

            # Skip year=0 entries — these are TIMES IE option codes
            # (interpolation/extrapolation flags), not actual data values.
            if year == "0" or year == "0.0":
                continue

            # Create nested dict structure
            if attr not in self._data:
                self._data[attr] = {}

            key = (scenario, process, year)
            try:
                self._data[attr][key] = float(val)
            except (ValueError, TypeError):
                pass

            # Track source file for traceability
            if has_source:
                src = str(src).strip()
                if src:
                    if attr not in self._source:
                        self._source[attr] = {}
                    self._source[attr][key] = src

    def _apply_scenario_precedence(self) -> None:
        """Collapse multi-scenario entries to highest-precedence per (attr, process, year).

        Scenarios absent from the run's DD list are dropped entirely, matching the
        behaviour of ``_extract_coeff_from_base_data_long`` in cost_extraction.
        """
        for attr in list(self._data):
            best: dict[tuple[str, str], tuple[int, str, float]] = {}
            best_src: dict[tuple[str, str], str] = {}
            src_map = self._source.get(attr, {})
            for (scenario, process, year), val in self._data[attr].items():
                sc_lower = scenario.lower()
                if sc_lower not in self._scenario_order:
                    continue  # Not in the run's DD list → exclude
                order = self._scenario_order[sc_lower]
                key = (process, year)
                if key not in best or order > best[key][0]:
                    best[key] = (order, scenario, val)
                    src = src_map.get((scenario, process, year), "")
                    if src:
                        best_src[key] = src
            self._data[attr] = {
                ("BASE", process, year): val
                for (process, year), (_order, _scenario, val) in best.items()
            }
            # Rebuild source mapping with BASE keys
            new_src = {}
            for (process, year), src in best_src.items():
                new_src[("BASE", process, year)] = src
            if new_src:
                self._source[attr] = new_src
            elif attr in self._source:
                del self._source[attr]

    def get(self, attribute: str, process: str, year: int | None = None, scenario: str = "BASE") -> float | None:
        """
        Get reference value for (attribute, scenario, process, year).

        With proper TIMES I/E applied upstream by
        ``_apply_times_interpolation`` in dd_inputs.py, the effective CSV
        already contains interpolated/extrapolated values at all model
        periods.  This method therefore does exact lookups only.

        When *year* is None the first available value for the process is
        returned (used for year-independent attributes like NCAP_START).

        Parameters
        ----------
        attribute : str
            Attribute name (e.g., "NCAP_TLIFE", "NCAP_DRATE").
        process : str
            Process name.
        year : int, optional
            Year to look up.  If None, returns first available value.
        scenario : str, optional
            Scenario name (default: "BASE").

        Returns
        -------
        float or None
            Value if found, else None.
        """
        if attribute not in self._data:
            return None

        attr_data = self._data[attribute]

        # Exact (scenario, process, year) lookup
        if year is not None:
            key = (scenario, process, str(year))
            if key in attr_data:
                return attr_data[key]

        entries = self._by_process.get(attribute, {}).get(process, ())

        # Year-independent lookup: return any entry for this process
        if year is None:
            for key, val in entries:
                if key[0] == scenario:
                    return val

        # Cross-scenario fallback for exact year
        if year is not None:
            year_str = str(year)
            for key, val in entries:
                if key[2] == year_str:
                    return val

        # Last resort: any entry for this process (any scenario, any year)
        if entries:
            return entries[0][1]

        return None

    def get_source(self, attribute: str, process: str, year: int | None = None, scenario: str = "BASE") -> str:
        """Get the source DD file name for a value returned by ``get()``."""
        if attribute not in self._source:
            return ""
        src_map = self._source[attribute]
        entries = self._src_by_process.get(attribute, {}).get(process, ())
        if year is not None:
            key = (scenario, process, str(year))
            if key in src_map:
                return src_map[key]
            # Cross-scenario fallback
            year_str = str(year)
            for k, v in entries:
                if k[2] == year_str:
                    return v
        # Any entry for process
        if entries:
            return entries[0][1]
        return ""

    def get_gdrate(self, year: int = 2010) -> float:
        """
        Get the general discount rate (G_DRATE).

        Falls back across scenarios: tries 'SysSettings', 'BASE', then any.
        Returns 0.035 (3.5%) as ultimate fallback.
        """
        if "G_DRATE" not in self._data:
            return 0.035

        attr_data = self._data["G_DRATE"]

        # G_DRATE rows use process="-" in DD exports.
        for scenario in ["SysSettings", "BASE"]:
            key = (scenario, "-", str(year))
            if key in attr_data:
                return attr_data[key]

        # Fallback: any G_DRATE entry
        for key, val in attr_data.items():
            return val

        return 0.035

    def get_ref_year(self, default: int = 2020) -> int:
        """Get objective reference year (G_DYEAR) when available."""
        if "G_DYEAR" not in self._data:
            return int(default)
        attr_data = self._data["G_DYEAR"]
        for scenario in ["SysSettings", "BASE"]:
            for (sc, _proc, _yr), val in attr_data.items():
                if sc == scenario:
                    try:
                        return int(float(val))
                    except (ValueError, TypeError):
                        pass
        for (_sc, _proc, _yr), val in attr_data.items():
            try:
                return int(float(val))
            except (ValueError, TypeError):
                continue
        return int(default)

    def get_profile_info(self, process: str, year: int = 2010) -> dict:
        """
        Get investment profile (hurdle rate, economic life, technical life, lead time).

        Parameters
        ----------
        process : str
            Process name.
        year : int, optional
            Default year to query.

        Returns
        -------
        dict
            Keys: drate_pct (%), elife_years, tlife_years, iled_years, gdrate

        Fallback logic:
            - NCAP_DRATE: if missing, falls back to G_DRATE (social discount rate)
            - NCAP_ELIFE: if missing, falls back to NCAP_TLIFE
            - NCAP_TLIFE: if missing, no fallback (required)
            - NCAP_ILED: if missing, defaults to 0 (no lead time)
        """
        cache_key = (process, year)
        if cache_key in self._profile_cache:
            return self._profile_cache[cache_key]
        result = {}

        # General discount rate (always available as reference)
        gdrate = self.get_gdrate(year)
        result["gdrate"] = gdrate

        # Technology-specific hurdle rate
        drate = self.get("NCAP_DRATE", process, year)
        if drate is not None:
            result["drate_pct"] = drate * 100
            src = self.get_source("NCAP_DRATE", process, year)
            result["drate_source"] = f"NCAP_DRATE (from {src})" if src else "NCAP_DRATE"
        else:
            # Fallback to general discount rate
            result["drate_pct"] = gdrate * 100
            result["drate_source"] = "G_DRATE (fallback)"

        # Technical life — DD files are pre-interpolated so get() returns
        # the correct value directly.  Use round() not int() because TIMES
        # rounds fractional TLIFE (e.g. 14.989 → 15, not 14).
        tlife = self.get("NCAP_TLIFE", process, year)
        if tlife is not None:
            result["tlife_years"] = round(tlife)

        # Economic life (fallback to TLIFE)
        elife = self.get("NCAP_ELIFE", process, year)
        if elife is not None:
            result["elife_years"] = round(elife)
            result["elife_source"] = "NCAP_ELIFE"
        elif tlife is not None:
            result["elife_years"] = round(tlife)
            result["elife_source"] = "NCAP_TLIFE (fallback)"

        # Construction lead time (ILED)
        # Negative value = years of advance build time required
        iled = self.get("NCAP_ILED", process, year)
        if iled is not None:
            result["iled_years"] = int(iled)
        else:
            result["iled_years"] = 0

        # Earliest feasible build year (NCAP_START).
        # NCAP_START has no year dimension — look up without year to get the
        # process-level value.
        ncap_start = self.get("NCAP_START", process)
        if ncap_start is not None:
            try:
                result["ncap_start"] = int(float(ncap_start))
            except (ValueError, TypeError):
                result["ncap_start"] = None
        else:
            result["ncap_start"] = None

        self._profile_cache[cache_key] = result
        return result


# ---------------------------------------------------------------------------
# Cost Reconciliation (Input vs Inferred)
# ---------------------------------------------------------------------------

def _capex_interpolated_at_year(capex_row: pd.DataFrame, target_year: int) -> float | None:
    """Linearly interpolate NCAP_COST at an arbitrary year.

    For Case 2a, TIMES uses NCAP_COST at B(t) = M(t) - floor(D/2), which
    may fall between model-period years.  This function linearly interpolates
    between the two nearest year-columns in the capex table, matching TIMES's
    IE-10 interpolation behaviour for NCAP_COST.

    Falls back to nearest year if target_year is outside the range.
    """
    if capex_row.empty:
        return None

    row = capex_row.iloc[0]
    data_points: dict[int, float] = {}
    for col in capex_row.columns:
        if not str(col).isdigit():
            continue
        v = pd.to_numeric(row[col], errors="coerce")
        if pd.notna(v):
            data_points[int(col)] = float(v)
    if not data_points:
        return None

    # Exact match
    if target_year in data_points:
        return data_points[target_year]

    known_years = sorted(data_points.keys())

    # Before first known year or after last — use nearest
    if target_year <= known_years[0]:
        return data_points[known_years[0]]
    if target_year >= known_years[-1]:
        return data_points[known_years[-1]]

    # Linear interpolation between bracketing years
    for i in range(len(known_years) - 1):
        y_lo, y_hi = known_years[i], known_years[i + 1]
        if y_lo <= target_year <= y_hi:
            frac = (target_year - y_lo) / (y_hi - y_lo)
            return data_points[y_lo] + frac * (data_points[y_hi] - data_points[y_lo])

    # Should not reach here
    nearest = min(known_years, key=lambda k: abs(k - target_year))
    return data_points[nearest]


def _capex_avg_over_spread(capex_row: pd.DataFrame, milestone: int, period_duration: int) -> float | None:
    """Average NCAP_COST over the OBLONG investment spread years.

    For Case 1a, TIMES spreads investment uniformly over D years starting at
    B(t) = M(t) - FLOOR(D(t)/2).  When NCAP_COST varies across these years,
    TIMES uses the average of NCAP_COST(y) for y in [B, B+D-1].

    DD files contain TIMES interpolations at model-period years.  For years
    between model periods (e.g. 2018 between 2015 and 2020), TIMES linearly
    interpolates between the bracketing data years — matching the I/E
    convention used for parameter lookup at arbitrary years.

    Falls back to the milestone-year value when D <= 1 or the capex row is
    empty.
    """
    if capex_row.empty or period_duration <= 1:
        return _capex_value(capex_row, milestone)

    row = capex_row.iloc[0]
    data_points: dict[int, float] = {}
    for col in capex_row.columns:
        if not str(col).isdigit():
            continue
        v = pd.to_numeric(row[col], errors="coerce")
        if pd.notna(v):
            data_points[int(col)] = float(v)
    if not data_points:
        return None

    known_years = sorted(data_points.keys())

    B = _oblong_B(milestone, period_duration)
    total = 0.0
    for y in range(B, B + period_duration):
        val = data_points.get(y)
        if val is None:
            # Linear interpolation between bracketing data years,
            # matching TIMES's I/E convention for arbitrary-year lookup.
            if y <= known_years[0]:
                val = data_points[known_years[0]]
            elif y >= known_years[-1]:
                val = data_points[known_years[-1]]
            else:
                for i in range(len(known_years) - 1):
                    y_lo, y_hi = known_years[i], known_years[i + 1]
                    if y_lo <= y <= y_hi:
                        frac = (y - y_lo) / (y_hi - y_lo)
                        val = data_points[y_lo] + frac * (data_points[y_hi] - data_points[y_lo])
                        break
                if val is None:
                    nearest = min(known_years, key=lambda k: abs(k - y))
                    val = data_points[nearest]
        total += val
    return total / period_duration


def _normalise_capex_table(input_costs: dict[str, pd.DataFrame] | None) -> pd.DataFrame:
    if input_costs is None:
        return pd.DataFrame()
    capex_df = input_costs.get("capex", pd.DataFrame())
    if capex_df.empty:
        return pd.DataFrame()
    if "Process" not in capex_df.columns:
        capex_df = capex_df.reset_index()
    if "process" in capex_df.columns and "Process" not in capex_df.columns:
        capex_df = capex_df.rename(columns={"process": "Process"})
    capex_df = capex_df.copy()
    capex_df["Process"] = capex_df["Process"].astype(str).str.strip()
    return capex_df


def _capex_value(capex_row: pd.DataFrame, period: int) -> float | None:
    if capex_row.empty:
        return None
    col = str(int(period))
    if col not in capex_row.columns:
        return None
    v = pd.to_numeric(capex_row.iloc[0][col], errors="coerce")
    if pd.isna(v):
        return None
    return float(v)


def _capex_periods(capex_row: pd.DataFrame) -> list[int]:
    """Return year-columns with available NCAP_COST values for this process."""
    if capex_row.empty:
        return []

    out: list[int] = []
    row = capex_row.iloc[0]
    for col in capex_row.columns:
        if not str(col).isdigit():
            continue
        v = pd.to_numeric(row[col], errors="coerce")
        if pd.isna(v):
            continue
        out.append(int(col))
    return sorted(set(out))


def reconcile_cost_inv(
    vd: pd.DataFrame,
    input_costs: dict[str, pd.DataFrame],
    scenario: str | None = None,
    ref_data: ReferenceData | None = None,
    period_durations: dict[int, int] | None = None,
) -> pd.DataFrame:
    """
    Build period-level reconciliation between NCAP_COST inputs and Cost_Inv outputs.

    Returns one row per (Process, Period) with forward (expected Cost_Inv from vintages)
    and reverse (implied NCAP_COST from Cost_Inv) diagnostics.

    Parameters
    ----------
    period_durations : dict[int, int], optional
        Mapping of milestone year → period duration D(t).  Used for
        investment case classification (Guide §4.4).

        The annuity formula is ``VAR_Ncap × NCAP_COST × CRF``.  Although
        TIMES spreads new capacity progressively across D(t) years, the
        Cost_Inv reported in VD files at each period already reflects the
        accumulated annuities from all D(t) progressive tranches, so the
        D(t) factors cancel out and no spread divisor is applied.

        If not provided, D(t)=1 is assumed for investment case classification.
    """
    if vd.empty:
        return pd.DataFrame()

    work = vd.copy()
    if scenario is not None and "Scenario" in work.columns:
        work = work[work["Scenario"] == scenario].copy()
    if work.empty:
        return pd.DataFrame()

    capex_df = _normalise_capex_table(input_costs)

    # Normalize attribute labels from VD (case and underscore variations).
    attr_norm = work["Attribute"].astype(str).str.strip().str.upper()

    # Investment reconciliation is driven by VAR_Ncap (new build decisions).
    # VAR_Cap is a stock variable used for fixed O&M and capacity constraints,
    # not for deriving build vintages behind Cost_Inv annuities.
    ncap = work[attr_norm.isin(["VAR_NCAP", "VAR_NCAPT", "VAR_NCAP_R"])].copy()
    cap = work[attr_norm.isin(["VAR_CAP", "VAR_CAPT"])].copy()
    inv = work[attr_norm == "COST_INV"].copy()

    for tbl in (ncap, cap, inv):
        if tbl.empty:
            continue
        tbl["Process"] = tbl["Process"].astype(str).str.strip()
        tbl["Period"] = pd.to_numeric(tbl["Period"], errors="coerce")
        tbl.dropna(subset=["Period"], inplace=True)
        tbl["Period"] = tbl["Period"].astype(int)

    ncap_map = {}
    if not ncap.empty:
        ncap_map = (
            ncap.groupby(["Process", "Period"], dropna=False)["Value"]
            .sum()
            .to_dict()
        )

    cap_map = {}
    if not cap.empty:
        cap_map = (
            cap.groupby(["Process", "Period"], dropna=False)["Value"]
            .sum()
            .to_dict()
        )

    inv_map = {}
    if not inv.empty:
        inv_map = (
            inv.groupby(["Process", "Period"], dropna=False)["Value"]
            .sum()
            .to_dict()
        )

    # Only reconcile processes that actually have Cost_Inv output from the
    # solver.  Processes with only VAR_Ncap or only NCAP_COST (DD input) but
    # no Cost_Inv produce mostly-blank rows.
    process_set = {k[0] for k in inv_map.keys()}

    rows: list[dict] = []

    ref_year = int(min(all_periods_global)) if (all_periods_global := sorted({k[1] for k in ncap_map.keys()} | {k[1] for k in inv_map.keys()} | {k[1] for k in cap_map.keys()})) else 2020
    if ref_data is not None and hasattr(ref_data, "get_ref_year"):
        ref_year = ref_data.get_ref_year(default=ref_year)

    # Group once per process instead of scanning every (process, period) key
    # and re-filtering the capex table inside the process loop.
    def _by_process(period_map: dict) -> dict[str, dict[int, float]]:
        grouped: dict[str, dict[int, float]] = {}
        for (proc, period), value in period_map.items():
            grouped.setdefault(proc, {})[period] = value
        return grouped

    ncap_by_proc = _by_process(ncap_map)
    cap_by_proc = _by_process(cap_map)
    inv_by_proc = _by_process(inv_map)
    capex_by_proc = (
        dict(tuple(capex_df.groupby("Process", sort=False)))
        if not capex_df.empty else {}
    )
    empty_capex = capex_df.iloc[0:0] if not capex_df.empty else pd.DataFrame()

    for process in sorted(process_set):
        proc_capex = capex_by_proc.get(process, empty_capex)
        has_suffix_00 = str(process).strip().endswith("00")
        # A process is "legacy" if it carries Cost_Inv but has no new-build
        # decisions (VAR_Ncap).  The legacy proxy uses VAR_Cap instead.
        proc_ncap = ncap_by_proc.get(process, {})
        has_ncap_builds = any(abs(float(v)) > 1e-12 for v in proc_ncap.values())
        is_legacy_process = not has_ncap_builds

        build_periods = sorted({p for p, v in proc_ncap.items() if abs(float(v)) > 1e-12})
        cap_periods = sorted({p for p, v in cap_by_proc.get(process, {}).items() if abs(float(v)) > 1e-12})
        inv_periods = sorted({p for p, v in inv_by_proc.get(process, {}).items() if abs(float(v)) > 1e-12})

        # Show NCAP_COST trajectory years for technologies that appear in VD,
        # so missing years in reconciliation are visible to users.
        has_vd_periods = bool(build_periods or inv_periods)
        capex_periods = _capex_periods(proc_capex) if has_vd_periods else []
        all_periods = sorted(set(build_periods) | set(cap_periods) | set(inv_periods) | set(capex_periods))
        # Filter out artificial far-future years (e.g. 2200) from capex
        # columns that are not real model periods.  Keep only years that
        # appear in VD or in the period_durations mapping.
        if period_durations:
            vd_years = set(build_periods) | set(cap_periods) | set(inv_periods)
            all_periods = sorted(
                p for p in all_periods
                if p in period_durations or p in vd_years
            )
        if not all_periods:
            continue

        vintage_profiles: dict[int, dict] = {}
        capex_values: list[float] = []
        has_nonzero_iled = False

        # End-of-horizon is the last calendar year of the last period
        # (inclusive), not the last milestone.  Under OBLONG:
        #   E(t) = B(t) + D(t) - 1 = M(t) + ceil(D(t)/2) - 1
        last_milestone = max(all_periods)
        if period_durations and last_milestone in period_durations:
            D_last = period_durations[last_milestone]
            eoh_year = _oblong_B(last_milestone, D_last) + D_last - 1
        else:
            eoh_year = last_milestone

        # NCAP_START: earliest feasible build year for this process.
        # Vintages before this year are spurious and skipped to avoid
        # inflating expected_cost_inv with phantom build decisions.
        proc_ncap_start: int | None = None
        if ref_data is not None:
            ns_val = ref_data.get("NCAP_START", process)
            if ns_val is not None:
                try:
                    proc_ncap_start = int(float(ns_val))
                except (ValueError, TypeError):
                    pass

        for vp in build_periods:
            if proc_ncap_start is not None and int(vp) < proc_ncap_start:
                continue
            built_v = float(ncap_map.get((process, vp), 0.0))
            ncap_cost_v = _capex_value(proc_capex, vp)
            profile = ref_data.get_profile_info(process, int(vp)) if ref_data else {}

            drate_pct = profile.get("drate_pct")
            elife = profile.get("elife_years")
            tlife = profile.get("tlife_years")
            iled = profile.get("iled_years", 0)

            if pd.notna(iled) and float(iled) != 0.0:
                has_nonzero_iled = True

            gdrate_v = (
                ref_data.get_gdrate(int(vp))
                if ref_data is not None and hasattr(ref_data, "get_gdrate")
                else 0.035
            )
            # Investment case classification (TIMES Part II §6.2)
            tlife_i = round(tlife) if tlife is not None and pd.notna(tlife) else 0
            iled_f = float(iled) if iled is not None and pd.notna(iled) else 0.0
            D_t = period_durations.get(vp, 1) if period_durations else 1

            inv_case = classify_investment_case(iled_f, tlife_i if tlife_i > 0 else None, D_t)

            # Case 1a within-period NCAP_COST averaging: TIMES spreads
            # investment over D years and uses the average NCAP_COST across
            # the spread window [B, B+D-1] rather than the milestone value.
            if inv_case == "case_1a" and D_t > 1:
                avg_cost = _capex_avg_over_spread(proc_capex, int(vp), D_t)
                if avg_cost is not None:
                    ncap_cost_v = avg_cost

            # Case 2a/2b NCAP_COST at B(t): TIMES uses NCAP_COST
            # interpolated at the period start year B(t) = M(t) − ⌊D/2⌋,
            # not at the milestone year (Part II §6.2, Case 2.a formula
            # uses NCAP_COST_{B(t)}).  When B(t) falls between model
            # periods, TIMES linearly interpolates (IE-10 convention).
            if inv_case in ("case_2a", "case_2b") and D_t > 1:
                B_cost = _oblong_B(int(vp), D_t)
                if B_cost != int(vp):
                    interp_cost = _capex_interpolated_at_year(proc_capex, B_cost)
                    if interp_cost is not None:
                        ncap_cost_v = interp_cost

            tlife_for_spread = tlife_i if tlife_i > 0 else 10

            # Full-vintage annualized Cost_Inv via unified formula:
            #   NCAP_COST × VAR_Ncap × CRF(drate, ELIFE)
            #   × CRF(gdrate, TLIFE)/CRF(gdrate, ELIFE)   [re-levelization]
            #   × (1+drate)^(|ILED|/2)                    [IDC, Case 2 only]
            drate_v = float(drate_pct) / 100.0 if drate_pct is not None else gdrate_v
            elife_for_crf = round(elife) if elife is not None and pd.notna(elife) and round(elife) > 0 else tlife_for_spread

            annuity = None
            if ncap_cost_v is not None:
                annuity = compute_vintage_annuity(
                    ncap_cost=float(ncap_cost_v),
                    var_ncap=float(built_v),
                    drate=drate_v,
                    gdrate=gdrate_v,
                    elife=elife_for_crf,
                    tlife=tlife_for_spread,
                    inv_case=inv_case,
                    iled=iled_f,
                )
                if annuity is not None and float(ncap_cost_v) > 0:
                    capex_values.append(float(ncap_cost_v))
            crf = annuity / (float(ncap_cost_v) * float(built_v)) if (
                annuity is not None and ncap_cost_v is not None
                and float(ncap_cost_v) > 0 and float(built_v) > 0
            ) else None

            # Pay fraction using OBLONG discounted partial-period proration.
            # Under $SET OBLONG YES:  B(t) = M(t) - FLOOR(D(t)/2)
            #
            # Case 1a: single operational window [B_v, B_v+TLIFE).
            #   pay_frac = AF(g,k) / AF(g,D)  where k = overlap years.
            #
            # Case 2a/2b: single operational window [B_v, B_v+TLIFE).
            #   Under OBLONG, the single-block window is correct (not per-
            #   tranche); the IDC factor in the annuity accounts for
            #   construction timing.
            #
            # Case 1b: C = ceil(D_v/TLIFE) replacement cycles.  Each cycle
            #   starts at B_v + i*TLIFE and investment is spread over TLIFE
            #   years (progressive per-tranche investment).  We sum the
            #   per-tranche OBLONG pay fractions across all C cycles.
            elife_i = int(round(float(elife))) if elife is not None and pd.notna(elife) else tlife_for_spread
            D_v = period_durations.get(vp, 1) if period_durations else 1
            B_v = _oblong_B(int(vp), D_v)
            pay_frac_by_period: dict[int, float] = {}
            for py in all_periods:
                # A vintage's costs are only reported from its build period
                # onwards.  OBLONG may place B_v before the milestone year,
                # but TIMES never attributes Cost_Inv to earlier periods.
                if int(py) < int(vp):
                    pay_frac_by_period[int(py)] = 0.0
                    continue
                D_py = period_durations.get(int(py), 1) if period_durations else 1
                B_py = _oblong_B(int(py), D_py)
                E_py = B_py + D_py
                if inv_case == "case_1b" and tlife_for_spread > 0 and D_v > 0:
                    # Case 1b (repeated investment).
                    #
                    # When D_v is exactly divisible by TLIFE, all C cycles
                    # fit within the period and TIMES reports the full annuity
                    # (pay_frac = 1.0 at build period, 0.0 elsewhere).
                    #
                    # When D_v % TLIFE != 0, the last cycle's payments extend
                    # beyond the period boundary.  TIMES applies per-tranche
                    # OBLONG proration: each tranche-year within each cycle
                    # has its own discounted pay fraction AF(g,k)/AF(g,D).
                    C = max(1, math.ceil(D_v / tlife_for_spread))
                    all_cycles_fit = (C * tlife_for_spread <= D_v)

                    if all_cycles_fit:
                        # All cycles contained: pay_frac = 1.0 at build, 0 elsewhere.
                        if int(py) == int(vp):
                            pay_frac_by_period[int(py)] = 1.0
                        else:
                            pay_frac_by_period[int(py)] = 0.0
                    else:
                        # Last cycle spills: per-tranche progressive formula.
                        # Each increment at year v generates ELIFE years of
                        # payments [v, v+ELIFE), per TIMES Part II §6.2 I.1.b.
                        total_frac = 0.0
                        for ci in range(C):
                            cycle_B = B_v + ci * tlife_for_spread
                            for tr in range(tlife_for_spread):
                                tranche_yr = cycle_B + tr
                                tranche_end = tranche_yr + elife_i
                                k_i = max(0, min(tranche_end, E_py) - max(tranche_yr, B_py))
                                total_frac += _discounted_pay_frac(k_i, D_py, gdrate_v) / tlife_for_spread
                        pay_frac_by_period[int(py)] = total_frac
                else:
                    end_v = B_v + tlife_for_spread
                    k = max(0, min(end_v, E_py) - max(B_v, B_py))
                    pay_frac_by_period[int(py)] = _discounted_pay_frac(k, D_py, gdrate_v)

            salvage_proxy = 0.0
            if ncap_cost_v is not None and built_v > 0 and tlife_for_spread > 0 and eoh_year > 0:
                salvage_proxy = _estimate_salvage_proxy_for_vintage(
                    vintage_year=vp,
                    investment_case=inv_case,
                    tlife=float(tlife_for_spread),
                    iled=float(iled_f),
                    period_duration=float(D_t),
                    ncap_cost=float(ncap_cost_v),
                    var_ncap=float(built_v),
                    eoh_year=int(eoh_year),
                )

            vintage_profiles[vp] = {
                "var_ncap": built_v,
                "ncap_cost": ncap_cost_v,
                "drate_pct": drate_pct,
                "elife": elife,
                "elife_i": elife_i,
                "tlife": tlife_i,
                "iled": iled_f,
                "crf": crf,
                "annuity": annuity,
                "pay_frac_by_period": pay_frac_by_period,
                "salvage_proxy": salvage_proxy,
                "investment_case": inv_case,
            }

        ncap_variation_pct = 0.0
        if len(capex_values) >= 2:
            hi = max(capex_values)
            lo = min(capex_values)
            if hi > 0:
                ncap_variation_pct = (hi - lo) / hi * 100.0

        weighted_input_cost = None
        total_weight = 0.0
        weighted_sum = 0.0
        for vp, prof in vintage_profiles.items():
            if prof["ncap_cost"] is None or prof["var_ncap"] <= 0:
                continue
            total_weight += prof["var_ncap"]
            weighted_sum += float(prof["ncap_cost"]) * float(prof["var_ncap"])
        if total_weight > 0:
            weighted_input_cost = weighted_sum / total_weight

        legacy_ref_ncap_cost = None
        if is_legacy_process and not vintage_profiles and inv_periods:
            for rp in sorted(inv_periods):
                cap_v = float(cap_map.get((process, rp), 0.0))
                act_v = float(inv_map.get((process, rp), 0.0))
                if cap_v <= 1e-12 or abs(act_v) <= 1e-12:
                    continue
                profile_r = ref_data.get_profile_info(process, int(rp)) if ref_data else {}
                tlife_r_raw = profile_r.get("tlife_years")
                tlife_r_i = round(tlife_r_raw) if tlife_r_raw is not None and pd.notna(tlife_r_raw) and round(tlife_r_raw) > 0 else 10
                elife_r_raw = profile_r.get("elife_years")
                elife_r_i = round(elife_r_raw) if elife_r_raw is not None and pd.notna(elife_r_raw) and round(elife_r_raw) > 0 else tlife_r_i
                drate_r_raw = profile_r.get("drate_pct")
                if ref_data is not None and hasattr(ref_data, "get_gdrate"):
                    gdrate_r = ref_data.get_gdrate(int(rp))
                else:
                    gdrate_r = 0.035
                drate_r = float(drate_r_raw) / 100.0 if drate_r_raw is not None else gdrate_r
                crf_r = None
                try:
                    crf_r_h = calculate_crf(drate_r, elife_r_i)
                    if elife_r_i != tlife_r_i:
                        crf_r = crf_r_h * calculate_crf(gdrate_r, tlife_r_i) / calculate_crf(gdrate_r, elife_r_i)
                    else:
                        crf_r = crf_r_h
                except Exception:
                    crf_r = None
                if crf_r is not None and crf_r > 1e-12:
                    legacy_ref_ncap_cost = back_calc_ncap_cost(float(act_v) / cap_v, float(crf_r))
                    break

        for p in all_periods:
            has_actual_obs = (process, p) in inv_map
            actual_cost_inv = float(inv_map[(process, p)]) if has_actual_obs else None
            current_build = float(ncap_map.get((process, p), 0.0))
            current_cap = float(cap_map.get((process, p), 0.0))

            active_profiles = []
            for vp, prof in vintage_profiles.items():
                pay_frac = float(prof.get("pay_frac_by_period", {}).get(int(p), 0.0))
                if pay_frac <= 0.0:
                    continue
                active_profiles.append((vp, prof, pay_frac))

            expected_cost_inv = 0.0
            expected_terms = 0
            for _, prof, pay_frac in active_profiles:
                ann = prof["annuity"]
                if ann is None:
                    continue
                expected_cost_inv += float(ann) * float(pay_frac)
                expected_terms += 1
            expected_available = expected_terms > 0

            expected_salvage_proxy = float(sum(float(prof.get("salvage_proxy", 0.0)) for prof in vintage_profiles.values()))

            input_cost_current = _capex_value(proc_capex, p)
            profile_p = ref_data.get_profile_info(process, int(p)) if ref_data else {}
            tlife_p_raw = profile_p.get("tlife_years")
            tlife_p_i = round(tlife_p_raw) if tlife_p_raw is not None and pd.notna(tlife_p_raw) and round(tlife_p_raw) > 0 else 10
            elife_p_raw = profile_p.get("elife_years")
            elife_p_i = round(elife_p_raw) if elife_p_raw is not None and pd.notna(elife_p_raw) and round(elife_p_raw) > 0 else tlife_p_i
            drate_p_raw = profile_p.get("drate_pct")

            # gdrate_p used for levelization and discount factor.
            if ref_data is not None and hasattr(ref_data, "get_gdrate"):
                gdrate_p = ref_data.get_gdrate(int(p))
            else:
                gdrate_p = 0.035
            drate_p = float(drate_p_raw) / 100.0 if drate_p_raw is not None else gdrate_p

            # Back-calculate implied NCAP_COST using the same levelization
            # formula: CRF(drate, elife) × CRF(gdrate, tlife) / CRF(gdrate, elife)
            crf_p = None
            try:
                crf_p_hurdle = calculate_crf(drate_p, elife_p_i)
                if elife_p_i != tlife_p_i:
                    crf_p = crf_p_hurdle * calculate_crf(gdrate_p, tlife_p_i) / calculate_crf(gdrate_p, elife_p_i)
                else:
                    crf_p = crf_p_hurdle
            except Exception:
                crf_p = None

            implied_ncap_cost = None
            capacity_driver = current_build if current_build > 1e-12 else current_cap
            if has_actual_obs and capacity_driver > 1e-12 and crf_p is not None and crf_p > 0:
                implied_ncap_cost = back_calc_ncap_cost(float(actual_cost_inv) / capacity_driver, float(crf_p))

            legacy_proxy_used = False
            if (
                (not expected_available or current_build <= 1e-12)
                and is_legacy_process
                and legacy_ref_ncap_cost is not None
                and current_cap > 1e-12
                and crf_p is not None
                and crf_p > 0
            ):
                legacy_expected = float(legacy_ref_ncap_cost) * float(current_cap) * float(crf_p)
                expected_cost_inv = max(float(expected_cost_inv), legacy_expected)
                expected_available = True
                legacy_proxy_used = True

            gap_pct = None
            if has_actual_obs and expected_available:
                if abs(expected_cost_inv) > 1e-9:
                    gap_pct = ((float(actual_cost_inv) - expected_cost_inv) / expected_cost_inv) * 100.0
                elif abs(float(actual_cost_inv)) > 1e-9:
                    # Expected is ~0 but actual is nonzero → large mismatch
                    gap_pct = 100.0 if float(actual_cost_inv) > 0 else -100.0
                else:
                    # Both actual and expected are ~0 → no meaningful gap
                    gap_pct = 0.0
            discount_factor = _discount_factor(int(p), ref_year, float(gdrate_p))
            expected_cost_inv_npv = (expected_cost_inv * discount_factor) if expected_available else None
            cost_inv_vd_npv = (float(actual_cost_inv) * discount_factor) if has_actual_obs else None

            # --- Reason classification ---
            # Two categories: MATCH (≤5%) and MISMATCH (>5% or missing data
            # for a process that has real Cost_Inv).
            if not expected_available and has_actual_obs and abs(float(actual_cost_inv)) > 0.01:
                # Actual Cost_Inv exists but no NCAP_COST input → can't reconcile
                reason = "MISMATCH"
            elif not expected_available or not has_actual_obs:
                # Both sides missing or zero → nothing to compare
                reason = "MATCH"
            elif gap_pct is not None and abs(gap_pct) <= 5.0:
                reason = "MATCH"
            else:
                reason = "MISMATCH"

            # Determine dominant investment case from active vintages
            inv_cases = [prof.get("investment_case", "unknown") for _, prof, _ in active_profiles]
            if inv_cases:
                # Pick the most common case among active vintages
                dominant_case = max(set(inv_cases), key=inv_cases.count)
            elif vintage_profiles:
                # No active vintages for this period, but vintages exist —
                # pick the most common case across all vintages.
                all_cases = [prof.get("investment_case", "unknown") for prof in vintage_profiles.values()]
                dominant_case = max(set(all_cases), key=all_cases.count)
            else:
                # No vintage profiles at all (e.g. legacy-only) — classify
                # using defaults from ref_data (ILED=0, TLIFE from DD or G_TLIFE=10).
                tlife_fallback = profile_p.get("tlife_years")
                iled_fallback = float(profile_p.get("iled_years", 0))
                D_t_fallback = period_durations.get(p, 1) if period_durations else 1
                dominant_case = classify_investment_case(iled_fallback, tlife_fallback, D_t_fallback)

            rows.append({
                "process": process,
                "period": int(p),
                "cost_inv_vd": float(actual_cost_inv) if has_actual_obs else None,
                "expected_cost_inv": expected_cost_inv if expected_available else None,
                "cost_inv_gap_pct": gap_pct,
                "var_ncap_current": current_build,
                "var_cap_current": current_cap,
                "capacity_driver_current": capacity_driver,
                "input_ncap_cost_current": input_cost_current,
                "implied_ncap_cost": implied_ncap_cost,
                "n_active_vintages": len(active_profiles) + (1 if legacy_proxy_used else 0),
                "n_build_vintages": len(vintage_profiles) if len(vintage_profiles) > 0 else (1 if legacy_proxy_used else 0),
                "weighted_input_cost": weighted_input_cost,
                "ncap_cost_variation_pct": ncap_variation_pct,
                "has_nonzero_iled": has_nonzero_iled,
                "investment_case": dominant_case,
                "build_segment": "legacy" if is_legacy_process or has_suffix_00 else "new",
                "legacy_proxy_used": legacy_proxy_used,
                "reference_year": int(ref_year),
                "social_drate": float(gdrate_p),
                "discount_factor": float(discount_factor),
                "expected_cost_inv_npv": expected_cost_inv_npv,
                "cost_inv_vd_npv": cost_inv_vd_npv,
                "expected_salvage_proxy": expected_salvage_proxy,
                "reason": reason,
            })

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["process", "period"]).reset_index(drop=True)


def build_vintage_table(
    vd: pd.DataFrame,
    input_costs: dict[str, pd.DataFrame],
    process: str,
    scenario: str | None = None,
    ref_data: ReferenceData | None = None,
    period_durations: dict[int, int] | None = None,
) -> pd.DataFrame:
    """Return per-vintage parameters used for Cost_Inv reconciliation."""
    if vd.empty or not process:
        return pd.DataFrame()

    work = vd.copy()
    if scenario is not None and "Scenario" in work.columns:
        work = work[work["Scenario"] == scenario].copy()

    # Vintage table for Cost_Inv must track VAR_Ncap build vintages only.
    ncap = work[
        (work["Attribute"] == "VAR_Ncap")
        & (work["Process"].astype(str).str.strip() == str(process).strip())
    ].copy()
    if ncap.empty:
        return pd.DataFrame()

    ncap["Period"] = pd.to_numeric(ncap["Period"], errors="coerce")
    ncap = ncap.dropna(subset=["Period"])
    ncap["Period"] = ncap["Period"].astype(int)
    built_by_year = (
        ncap.groupby("Period", dropna=False)["Value"].sum().to_dict()
    )

    capex_df = _normalise_capex_table(input_costs)
    proc_capex = capex_df[capex_df["Process"] == str(process).strip()] if not capex_df.empty else pd.DataFrame()

    rows = []
    for vintage in sorted(k for k, v in built_by_year.items() if abs(float(v)) > 1e-12):
        built = float(built_by_year.get(vintage, 0.0))
        ncap_cost = _capex_value(proc_capex, vintage)
        profile = ref_data.get_profile_info(process, int(vintage)) if ref_data else {}

        drate_pct = profile.get("drate_pct")
        elife = profile.get("elife_years")
        tlife = profile.get("tlife_years")
        iled = profile.get("iled_years", 0)

        # Investment case classification (TIMES Part II §6.2)
        tlife_i = round(tlife) if tlife is not None and pd.notna(tlife) else 0
        iled_f = float(iled) if iled is not None and pd.notna(iled) else 0.0
        D_t = period_durations.get(vintage, 1) if period_durations else 1

        inv_case = classify_investment_case(iled_f, tlife_i if tlife_i > 0 else None, D_t)

        # Case 1a within-period NCAP_COST averaging (same as reconcile_cost_inv).
        if inv_case == "case_1a" and D_t > 1:
            avg_cost = _capex_avg_over_spread(proc_capex, int(vintage), D_t)
            if avg_cost is not None:
                ncap_cost = avg_cost

        # Case 2a/2b: NCAP_COST at B(t) (same as reconcile_cost_inv).
        if inv_case in ("case_2a", "case_2b") and D_t > 1:
            B_cost = _oblong_B(int(vintage), D_t)
            if B_cost != int(vintage):
                interp_cost = _capex_interpolated_at_year(proc_capex, B_cost)
                if interp_cost is not None:
                    ncap_cost = interp_cost

        tlife_for_spread = tlife_i if tlife_i > 0 else 10
        elife_i = round(elife) if elife is not None and pd.notna(elife) else tlife_for_spread

        # Discount rates for this vintage year.
        gdrate_v = (
            ref_data.get_gdrate(int(vintage))
            if ref_data is not None and hasattr(ref_data, "get_gdrate")
            else 0.035
        )
        drate_v = float(drate_pct) / 100.0 if drate_pct is not None else gdrate_v

        # CRF for display: hurdle-rate × re-levelization factor (no IDC,
        # so users see the pure financing annuity factor in the table).
        crf = None
        if drate_pct is not None and elife is not None:
            try:
                crf_h = calculate_crf(drate_v, elife_i)
                if elife_i != tlife_for_spread and tlife_for_spread > 0:
                    crf_g_elife = calculate_crf(gdrate_v, elife_i)
                    crf_g_tlife = calculate_crf(gdrate_v, tlife_for_spread)
                    crf = crf_h * (crf_g_tlife / crf_g_elife) if crf_g_elife > 1e-15 else crf_h
                else:
                    crf = crf_h
            except Exception:
                crf = None

        # Annuity via unified formula (includes IDC for Case 2).
        annuity = None
        if ncap_cost is not None:
            annuity = compute_vintage_annuity(
                ncap_cost=float(ncap_cost),
                var_ncap=float(built),
                drate=drate_v,
                gdrate=gdrate_v,
                elife=elife_i,
                tlife=tlife_for_spread,
                inv_case=inv_case,
                iled=iled_f,
            )

        # Derive payment window from the same case-based tranche timing used
        # by reconciliation so UI "active" years cannot disagree with £M/yr.
        active_from = None
        active_until = None

        if elife_i > 0:
            weights = _investment_year_weights(
                vintage_year=int(vintage),
                investment_case=inv_case,
                tlife=float(tlife_for_spread),
                iled=float(iled_f),
                period_duration=float(D_t),
            )
            if weights:
                annual_start = min(int(y) for y in weights.keys())
                annual_end = max(int(y) + int(elife_i) - 1 for y in weights.keys())

                if period_durations:
                    active_periods = []
                    for py in sorted(period_durations.keys()):
                        # Match reconciliation: TIMES never reports
                        # Cost_Inv for periods before the build year.
                        if int(py) < int(vintage):
                            continue
                        frac = _payment_fraction_for_period(
                            vintage_year=int(vintage),
                            investment_case=inv_case,
                            tlife=float(tlife_for_spread),
                            iled=float(iled_f),
                            period_duration=float(D_t),
                            elife=float(elife_i),
                            period_year=int(py),
                        )
                        if frac > 0.0:
                            active_periods.append(int(py))
                    if active_periods:
                        active_from = min(active_periods)
                        active_until = max(active_periods)
                    else:
                        active_from = annual_start
                        active_until = annual_end
                else:
                    active_from = annual_start
                    active_until = annual_end

        rows.append({
            "process": str(process).strip(),
            "vintage": int(vintage),
            "var_ncap": built,
            "ncap_cost": ncap_cost,
            "ncap_drate_pct": drate_pct,
            "ncap_elife_years": elife,
            "ncap_tlife_years": tlife_i if tlife_i > 0 else None,
            "ncap_iled_years": iled_f if iled_f != 0.0 else None,
            "crf": crf,
            "investment_case": inv_case,
            "annuity_cost_inv": annuity,
            "active_from": active_from,
            "active_until": active_until,
        })

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("vintage").reset_index(drop=True)


def compute_reconciliation_summary(reconciliation: pd.DataFrame) -> pd.DataFrame:
    """Compute the process-level summary of a reconcile_cost_inv() DataFrame.

    Each process is still aggregated on its own period-sorted rows: a
    groupby-level sum would use a different floating-point summation order and
    could shift divergence figures in the last bits.
    """
    recon_df = reconciliation
    if recon_df.empty:
        return pd.DataFrame()

    out_rows = []
    for process, grp in recon_df.groupby("process"):
        g = grp.sort_values("period")

        weighted_input_cost = g["weighted_input_cost"].dropna().head(1)
        weighted_input_cost = float(weighted_input_cost.iloc[0]) if not weighted_input_cost.empty else None

        # ---- Period-level gap aggregation ----
        # Use the direct comparison (expected annuity sum vs actual Cost_Inv)
        # rather than the indirect back-calculated implied_ncap_cost.
        # Only consider periods where both expected and actual are available.
        gap_rows = g[
            g["expected_cost_inv"].notna()
            & (g["cost_inv_vd"].notna())
        ].copy()

        total_expected = 0.0
        total_actual = 0.0
        total_expected_npv = 0.0
        total_actual_npv = 0.0
        if not gap_rows.empty:
            total_expected = gap_rows["expected_cost_inv"].astype(float).sum()
            total_actual = gap_rows["cost_inv_vd"].astype(float).sum()
            if "expected_cost_inv_npv" in gap_rows.columns:
                total_expected_npv = gap_rows["expected_cost_inv_npv"].dropna().astype(float).sum()
            if "cost_inv_vd_npv" in gap_rows.columns:
                total_actual_npv = gap_rows["cost_inv_vd_npv"].dropna().astype(float).sum()

        # Divergence based on aggregate expected vs actual Cost_Inv across all
        # periods — this is the definitive reconciliation metric.
        divergence_pct = None
        if total_expected > 1e-9:
            divergence_pct = ((total_actual - total_expected) / total_expected) * 100.0

        divergence_npv_pct = None
        if total_expected_npv > 1e-9:
            divergence_npv_pct = ((total_actual_npv - total_expected_npv) / total_expected_npv) * 100.0

        # Also keep the old implied_cost_from_vd for backward compatibility
        single_rows = g[
            (g["n_active_vintages"] == 1)
            & (g["var_ncap_current"] > 1e-12)
            & (g["implied_ncap_cost"].notna())
        ].copy()

        implied_cost_from_vd = None
        if not single_rows.empty:
            w = single_rows["var_ncap_current"].astype(float)
            implied_cost_from_vd = float((single_rows["implied_ncap_cost"].astype(float) * w).sum() / w.sum())

        # Summary-level reason: MATCH (≤5%) vs MISMATCH (>5% or missing
        # input data for a process with real Cost_Inv).
        total_vd_cost = float(g["cost_inv_vd"].dropna().astype(float).abs().sum()) if "cost_inv_vd" in g else 0.0
        if divergence_pct is None and total_vd_cost > 0.01:
            # Has actual Cost_Inv but no NCAP_COST to compare → MISMATCH
            reason = "MISMATCH"
        elif divergence_pct is None:
            reason = "MATCH"
        elif abs(divergence_pct) <= 5.0:
            reason = "MATCH"
        else:
            reason = "MISMATCH"

        n_vintages = int(g["n_build_vintages"].max()) if "n_build_vintages" in g else 0
        total_built_mw = 0.0
        if "var_ncap_current" in g and float(g["var_ncap_current"].abs().sum()) > 1e-12:
            total_built_mw = float(g["var_ncap_current"].sum())
        elif "var_cap_current" in g and float(g["var_cap_current"].abs().sum()) > 1e-12:
            # Legacy technologies carry stock in VAR_Cap with no new builds.
            total_built_mw = float(g["var_cap_current"].max())
        has_nonzero_iled = bool(g["has_nonzero_iled"].any()) if "has_nonzero_iled" in g else False
        build_segment = "new"
        if "build_segment" in g.columns and not g["build_segment"].dropna().empty:
            build_segment = str(g["build_segment"].mode().iloc[0])
        expected_salvage_proxy = None
        if "expected_salvage_proxy" in g.columns:
            vals = g["expected_salvage_proxy"].dropna()
            if not vals.empty:
                expected_salvage_proxy = float(vals.iloc[0])

        # Dominant investment case across periods
        inv_cases = g["investment_case"].dropna() if "investment_case" in g else pd.Series(dtype=str)
        inv_cases = inv_cases[inv_cases != "unknown"]
        if not inv_cases.empty:
            dominant_case = inv_cases.mode().iloc[0]
        else:
            dominant_case = "unknown"

        out_rows.append({
            "process": process,
            "weighted_input_cost": weighted_input_cost,
            "implied_cost_from_vd": implied_cost_from_vd,
            "divergence_pct": divergence_pct,
            "total_expected_cost_inv": total_expected if total_expected > 1e-9 else None,
            "total_actual_cost_inv": total_actual if total_actual > 1e-9 else (
                total_vd_cost if total_vd_cost > 1e-9 else None),
            "total_expected_cost_inv_npv": total_expected_npv if total_expected_npv > 1e-9 else None,
            "total_actual_cost_inv_npv": total_actual_npv if total_actual_npv > 1e-9 else None,
            "divergence_npv_pct": divergence_npv_pct,
            "expected_salvage_proxy": expected_salvage_proxy,
            "reason": reason,
            "multi_vintage": n_vintages > 1,
            "n_vintages": n_vintages,
            "total_built_mw": total_built_mw,
            "has_nonzero_iled": has_nonzero_iled,
            "investment_case": dominant_case,
            "build_segment": build_segment,
            # Legacy aliases retained for app compatibility.
            "input_cost_per_mw": weighted_input_cost,
            "inferred_cost_per_mw": implied_cost_from_vd,
            "iled_years": int(has_nonzero_iled),
        })

    if not out_rows:
        return pd.DataFrame()
    return pd.DataFrame(out_rows).sort_values("process").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Mismatch Root-Cause Diagnosis
# ---------------------------------------------------------------------------

_DIAGNOSIS_CAUSES = [
    "missing_data",
    "ncap_cost_mismatch",
    "drate_issue",
    "elife_tlife_issue",
    "idc_effect",
    "oblong_pay_frac",
    "multi_vintage",
    "case1a_averaging",
]


def diagnose_mismatch(
    process: str,
    recon_df: pd.DataFrame,
    ref_data: ReferenceData | None = None,
    period_durations: dict[int, int] | None = None,
    input_costs: dict[str, pd.DataFrame] | None = None,
) -> dict:
    """Diagnose root causes of divergence for a single process.

    Parameters
    ----------
    process : str
        Process name.
    recon_df : DataFrame
        Period-level reconciliation rows for this process (from reconcile_cost_inv).
    ref_data : ReferenceData, optional
        Reference data for parameter lookup.
    period_durations : dict, optional
        Milestone → D(t) mapping.
    input_costs : dict, optional
        Input cost tables (for NCAP_COST trajectory analysis).

    Returns
    -------
    dict with keys:
        process, divergence_pct, dominant_cause, gap_abs (£M total),
        cause_missing_data, cause_ncap_cost_mismatch, cause_drate_issue,
        cause_elife_tlife_issue, cause_idc_effect, cause_oblong_pay_frac,
        cause_multi_vintage, cause_case1a_averaging,
        detail (str explanation), contributions (dict cause→£M)
    """
    result = {
        "process": process,
        "divergence_pct": None,
        "dominant_cause": "unknown",
        "gap_abs": 0.0,
        "cause_missing_data": False,
        "cause_ncap_cost_mismatch": False,
        "cause_drate_issue": False,
        "cause_elife_tlife_issue": False,
        "cause_idc_effect": False,
        "cause_oblong_pay_frac": False,
        "cause_multi_vintage": False,
        "cause_case1a_averaging": False,
        "detail": "",
        "contributions": {},
    }

    if recon_df.empty:
        result["cause_missing_data"] = True
        result["dominant_cause"] = "missing_data"
        result["detail"] = "No reconciliation data available for this process."
        return result

    # Aggregate divergence
    valid = recon_df[recon_df["expected_cost_inv"].notna() & recon_df["cost_inv_vd"].notna()]
    total_expected = valid["expected_cost_inv"].astype(float).sum() if not valid.empty else 0.0
    total_actual = valid["cost_inv_vd"].astype(float).sum() if not valid.empty else 0.0
    gap_abs = total_actual - total_expected
    div_pct = (gap_abs / total_expected * 100.0) if total_expected > 1e-9 else None

    result["divergence_pct"] = div_pct
    result["gap_abs"] = gap_abs

    details: list[str] = []
    contributions: dict[str, float] = {}

    # --- Check 1: Missing DD data ---
    if ref_data is not None:
        profile = ref_data.get_profile_info(process)
        has_ncap_cost = recon_df["input_ncap_cost_current"].notna().any()
        has_drate = profile.get("drate_pct") is not None
        has_elife = profile.get("elife_years") is not None
        has_tlife = profile.get("tlife_years") is not None

        missing_params = []
        if not has_ncap_cost:
            missing_params.append("NCAP_COST")
        if not has_drate:
            missing_params.append("NCAP_DRATE")
        if not has_elife:
            missing_params.append("NCAP_ELIFE")
        if not has_tlife:
            missing_params.append("NCAP_TLIFE")

        if missing_params:
            result["cause_missing_data"] = True
            details.append(f"Missing DD parameters: {', '.join(missing_params)}")
            contributions["missing_data"] = gap_abs  # full gap attributed
    else:
        result["cause_missing_data"] = True
        details.append("No reference data available — cannot verify parameters.")
        contributions["missing_data"] = gap_abs

    # --- Check 2: NCAP_COST mismatch ---
    if not valid.empty and ref_data is not None:
        ncap_cost_rows = valid[
            valid["input_ncap_cost_current"].notna()
            & valid["implied_ncap_cost"].notna()
            & (valid["input_ncap_cost_current"].astype(float) > 1e-9)
        ]
        if not ncap_cost_rows.empty:
            inp = ncap_cost_rows["input_ncap_cost_current"].astype(float)
            imp = ncap_cost_rows["implied_ncap_cost"].astype(float)
            avg_inp = inp.mean()
            avg_imp = imp.mean()
            if avg_inp > 1e-9:
                cost_div = (avg_imp - avg_inp) / avg_inp * 100.0
                if abs(cost_div) > 1.0:  # even 1% is worth flagging
                    result["cause_ncap_cost_mismatch"] = True
                    # Estimate contribution: gap proportional to NCAP_COST deviation
                    cost_contribution = total_expected * (cost_div / 100.0)
                    contributions["ncap_cost_mismatch"] = cost_contribution
                    details.append(
                        f"NCAP_COST: input avg £{avg_inp:.3f}M vs solver-implied £{avg_imp:.3f}M "
                        f"({cost_div:+.1f}%). Explains ~£{cost_contribution:.2f}M of gap."
                    )

    # --- Check 3: DRATE issue ---
    if ref_data is not None:
        profile = ref_data.get_profile_info(process)
        drate_pct = profile.get("drate_pct")
        gdrate = profile.get("gdrate", 0.035)

        if drate_pct is None:
            result["cause_drate_issue"] = True
            details.append(
                f"NCAP_DRATE not found — G_DRATE fallback ({gdrate*100:.1f}%) used. "
                "If true hurdle rate differs, CRF changes and annuity shifts."
            )
            # Quantify: try a typical sector rate (8%) vs G_DRATE
            if total_expected > 1e-9:
                elife = profile.get("elife_years")
                if elife is not None and int(elife) > 0:
                    crf_fallback = calculate_crf(gdrate, int(elife))
                    crf_typical = calculate_crf(0.08, int(elife))
                    if crf_fallback > 1e-9:
                        drate_contribution = total_expected * (crf_typical / crf_fallback - 1.0)
                        contributions["drate_issue"] = drate_contribution
                        details.append(
                            f"  If true rate were 8%, gap would be ~£{drate_contribution:.2f}M "
                            f"({drate_contribution/total_expected*100:.1f}% of expected)."
                        )
        elif math.isclose(drate_pct, gdrate * 100.0, rel_tol=1e-9):
            # DRATE equals G_DRATE — might be a fallback scenario
            result["cause_drate_issue"] = True
            details.append(
                f"NCAP_DRATE = G_DRATE ({drate_pct:.1f}%) — possibly a fallback, "
                "not a true technology-specific rate."
            )

    # --- Check 4: ELIFE/TLIFE issue ---
    if ref_data is not None:
        profile = ref_data.get_profile_info(process)
        elife = profile.get("elife_years")
        tlife = profile.get("tlife_years")
        gdrate = profile.get("gdrate", 0.035)
        drate_pct = profile.get("drate_pct")

        if elife is not None and tlife is not None and int(elife) != int(tlife):
            result["cause_elife_tlife_issue"] = True
            # Quantify re-levelization contribution
            elife_i = int(elife)
            tlife_i = int(tlife)
            crf_g_e = calculate_crf(gdrate, elife_i)
            crf_g_t = calculate_crf(gdrate, tlife_i)
            relevel = (crf_g_t / crf_g_e) if crf_g_e > 1e-15 else 1.0
            relevel_effect = (relevel - 1.0) * total_expected
            contributions["elife_tlife_issue"] = relevel_effect
            details.append(
                f"ELIFE ({elife_i}yr) ≠ TLIFE ({tlife_i}yr) → re-levelization factor "
                f"= {relevel:.4f} (contributes £{relevel_effect:.2f}M, "
                f"{relevel_effect/total_expected*100:.1f}% of expected)."
            )
        elif elife is None and tlife is not None:
            result["cause_elife_tlife_issue"] = True
            details.append(
                f"NCAP_ELIFE missing — defaulting to NCAP_TLIFE ({int(tlife)}yr). "
                "If true economic life differs, CRF is wrong."
            )

    # --- Check 5: IDC effect (Case 2) ---
    inv_case = recon_df["investment_case"].mode()
    inv_case = inv_case.iloc[0] if not inv_case.empty else "unknown"
    has_iled = bool(recon_df["has_nonzero_iled"].any()) if "has_nonzero_iled" in recon_df else False

    if inv_case in ("case_2a", "case_2b") and has_iled and ref_data is not None:
        profile = ref_data.get_profile_info(process)
        iled = profile.get("iled_years", 0)
        gdrate = profile.get("gdrate", 0.035)
        iled_abs = abs(float(iled)) if iled is not None else 0.0
        if iled_abs > 0:
            idc_factor = (1.0 + gdrate) ** (iled_abs / 2.0)
            idc_contribution = total_expected * (idc_factor - 1.0)
            result["cause_idc_effect"] = True
            contributions["idc_effect"] = idc_contribution
            details.append(
                f"Case 2 IDC: ILED={iled_abs:.0f}yr, G_DRATE={gdrate*100:.1f}% → "
                f"IDC factor = (1+{gdrate:.3f})^({iled_abs:.0f}/2) = {idc_factor:.4f}. "
                f"Contributes £{idc_contribution:.2f}M ({idc_contribution/total_expected*100:.1f}%)."
            )

    # --- Check 6: OBLONG pay-fraction ---
    # Detect partial-period coverage from the period-level data.
    # If expected annuity is systematically lower than a simple
    # NCAP_COST × VAR_Ncap × CRF estimate, pay_frac < 1 is the cause.
    n_build = int(recon_df["n_build_vintages"].max()) if "n_build_vintages" in recon_df else 0
    if n_build > 0 and period_durations:
        # Check if any period has fewer active vintages than expected
        active_counts = recon_df["n_active_vintages"].dropna()
        build_counts = recon_df["n_build_vintages"].dropna()
        if not active_counts.empty and not build_counts.empty:
            max_builds = int(build_counts.max())
            periods_with_partial = int((active_counts < max_builds).sum())
            if periods_with_partial > 0 and max_builds > 1:
                result["cause_oblong_pay_frac"] = True
                details.append(
                    f"OBLONG partial-period: {periods_with_partial} period(s) have "
                    f"fewer active vintages ({int(active_counts.min())}–{int(active_counts.max())}) "
                    f"than total builds ({max_builds}). TLIFE doesn't span full period."
                )
        # Also check for D(t) > 1 with inv_case != case_1a (period fractions matter)
        if inv_case in ("case_1b", "case_2b"):
            result["cause_oblong_pay_frac"] = True
            if "OBLONG partial-period" not in " ".join(details):
                details.append(
                    f"Investment case {inv_case} involves repeated cycles within "
                    "the period — OBLONG pay fractions affect the sum."
                )

    # --- Check 7: Multi-vintage interference ---
    n_vintages = int(recon_df["n_build_vintages"].max()) if "n_build_vintages" in recon_df else 0
    if n_vintages > 1:
        # Check if divergence is non-uniform across periods (sign changes)
        gaps = recon_df["cost_inv_gap_pct"].dropna()
        if not gaps.empty and len(gaps) > 1:
            has_sign_change = (gaps.max() > 0 and gaps.min() < 0)
            variation = gaps.std()
            if has_sign_change or variation > 5.0:
                result["cause_multi_vintage"] = True
                details.append(
                    f"Multi-vintage process ({n_vintages} builds): period-level gap "
                    f"varies from {gaps.min():.1f}% to {gaps.max():.1f}% (σ={variation:.1f}%). "
                    "Overlapping vintages make exact decomposition difficult."
                )

    # --- Check 8: Case 1a within-period NCAP_COST averaging ---
    if inv_case == "case_1a" and input_costs is not None and ref_data is not None:
        capex_df = _normalise_capex_table(input_costs)
        proc_capex = capex_df[capex_df["Process"] == process] if not capex_df.empty else pd.DataFrame()
        if not proc_capex.empty:
            build_periods = recon_df[recon_df["var_ncap_current"] > 1e-12]["period"].tolist()
            for bp in build_periods:
                D_t = period_durations.get(int(bp), 1) if period_durations else 1
                if D_t <= 1:
                    continue
                milestone_cost = _capex_value(proc_capex, int(bp))
                avg_cost = _capex_avg_over_spread(proc_capex, int(bp), D_t)
                if milestone_cost is not None and avg_cost is not None and milestone_cost > 1e-9:
                    avg_div = (avg_cost - milestone_cost) / milestone_cost * 100.0
                    if abs(avg_div) > 1.0:
                        result["cause_case1a_averaging"] = True
                        details.append(
                            f"Case 1a averaging (period {bp}): milestone NCAP_COST "
                            f"= £{milestone_cost:.3f}M vs spread-window average "
                            f"= £{avg_cost:.3f}M ({avg_div:+.1f}%)."
                        )
                        contributions["case1a_averaging"] = (
                            contributions.get("case1a_averaging", 0.0)
                            + total_expected * (avg_div / 100.0) / max(len(build_periods), 1)
                        )
                        break  # one example suffices for the flag

    # --- Determine dominant cause ---
    # Rank by absolute contribution magnitude
    if contributions:
        dominant = max(contributions, key=lambda k: abs(contributions[k]))
        result["dominant_cause"] = dominant
    elif result["cause_missing_data"]:
        result["dominant_cause"] = "missing_data"
    elif result["cause_multi_vintage"]:
        result["dominant_cause"] = "multi_vintage"
    else:
        result["dominant_cause"] = "unknown"

    result["detail"] = "\n".join(details) if details else "No specific root cause identified."
    result["contributions"] = contributions
    return result


def diagnose_all_mismatches(
    recon_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    ref_data: ReferenceData | None = None,
    period_durations: dict[int, int] | None = None,
    input_costs: dict[str, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """Run root-cause diagnosis on ALL processes with any divergence.

    Parameters
    ----------
    recon_df : DataFrame
        Full period-level reconciliation output from reconcile_cost_inv().
    summary_df : DataFrame
        Process-level summary from compute_reconciliation_summary().
    ref_data : ReferenceData, optional
    period_durations : dict, optional
    input_costs : dict, optional

    Returns
    -------
    DataFrame with one row per diagnosed process, columns:
        process, divergence_pct, dominant_cause, gap_abs,
        cause_missing_data, cause_ncap_cost_mismatch, cause_drate_issue,
        cause_elife_tlife_issue, cause_idc_effect, cause_oblong_pay_frac,
        cause_multi_vintage, cause_case1a_averaging, detail
    """
    if summary_df.empty or recon_df.empty:
        return pd.DataFrame()

    div = pd.to_numeric(summary_df["divergence_pct"], errors="coerce")

    # Diagnose all processes with any divergence (not just >5%)
    candidates = summary_df[
        div.notna()
        & (div.abs() > 1e-6)
    ]["process"].tolist()

    # Also include processes flagged MISMATCH due to missing data
    missing_data_procs = summary_df[
        (summary_df["reason"] == "MISMATCH")
        & summary_df["divergence_pct"].isna()
    ]["process"].tolist()
    candidates = list(set(candidates + missing_data_procs))

    if not candidates:
        return pd.DataFrame()

    rows: list[dict] = []
    for proc in sorted(candidates):
        proc_recon = recon_df[recon_df["process"] == proc].copy()
        diag = diagnose_mismatch(
            proc, proc_recon,
            ref_data=ref_data,
            period_durations=period_durations,
            input_costs=input_costs,
        )
        # Flatten for DataFrame (exclude nested dict)
        row = {k: v for k, v in diag.items() if k != "contributions"}
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame(rows)
    out = out.sort_values(
        "divergence_pct",
        key=lambda s: pd.to_numeric(s, errors="coerce").abs(),
        ascending=False,
        na_position="last",
    )
    return out.reset_index(drop=True)


def sensitivity_decomposition(
    process: str,
    recon_df: pd.DataFrame,
    ref_data: ReferenceData | None = None,
    period_durations: dict[int, int] | None = None,
    input_costs: dict[str, pd.DataFrame] | None = None,
) -> list[tuple[str, float, str]]:
    """Compute waterfall sensitivity decomposition for a single process.

    Returns a list of (label, delta_£M, explanation) tuples representing
    successive "what-if" adjustments from our expected calculation toward
    the solver's actual value.

    The sum of all deltas should approximate the total gap (actual − expected).
    Any remainder is captured in a "Residual" step.

    Steps:
        1. Baseline (expected_cost_inv total)
        2. NCAP_COST effect
        3. DRATE / CRF effect
        4. ELIFE re-levelization effect
        5. IDC effect (Case 2 only)
        6. OBLONG / pay-frac effect
        7. Residual (unexplained)
    """
    steps: list[tuple[str, float, str]] = []

    if recon_df.empty:
        return [("No data", 0.0, "No reconciliation data for this process.")]

    valid = recon_df[recon_df["expected_cost_inv"].notna() & recon_df["cost_inv_vd"].notna()]
    if valid.empty:
        return [("No comparable periods", 0.0, "No periods with both expected and actual values.")]

    total_expected = valid["expected_cost_inv"].astype(float).sum()
    total_actual = valid["cost_inv_vd"].astype(float).sum()
    total_gap = total_actual - total_expected

    steps.append(("Expected (calculated)", total_expected,
                  f"Sum of annuity payments across all periods: £{total_expected:.2f}M"))

    if abs(total_gap) < 1e-6:
        steps.append(("Gap = 0", 0.0, "Perfect match — no gap to decompose."))
        return steps

    explained = 0.0
    profile = ref_data.get_profile_info(process) if ref_data else {}
    gdrate = profile.get("gdrate", 0.035)

    # Step 2: NCAP_COST contribution
    cost_rows = valid[
        valid["input_ncap_cost_current"].notna()
        & valid["implied_ncap_cost"].notna()
        & (valid["input_ncap_cost_current"].astype(float) > 1e-9)
    ]
    ncap_cost_delta = 0.0
    if not cost_rows.empty:
        inp = cost_rows["input_ncap_cost_current"].astype(float)
        imp = cost_rows["implied_ncap_cost"].astype(float)
        # Weighted average deviation
        weights = cost_rows["capacity_driver_current"].astype(float).clip(lower=1e-9)
        avg_inp = (inp * weights).sum() / weights.sum()
        avg_imp = (imp * weights).sum() / weights.sum()
        if avg_inp > 1e-9:
            ncap_cost_delta = total_expected * ((avg_imp - avg_inp) / avg_inp)
            explained += ncap_cost_delta
            steps.append((
                "NCAP_COST effect",
                ncap_cost_delta,
                f"Input avg £{avg_inp:.3f}M vs implied £{avg_imp:.3f}M → "
                f"ΔCost_Inv ≈ £{ncap_cost_delta:.2f}M"
            ))

    # Step 3: DRATE / CRF effect
    drate_delta = 0.0
    if ref_data is not None:
        drate_pct = profile.get("drate_pct")
        elife = profile.get("elife_years")
        if elife is not None and int(elife) > 0:
            elife_i = int(elife)
            # Our CRF uses drate (or G_DRATE fallback)
            our_drate = float(drate_pct) / 100.0 if drate_pct is not None else gdrate
            our_crf = calculate_crf(our_drate, elife_i)
            # Back-calculate the effective CRF from VD data
            # If implied_ncap_cost exists, effective CRF = actual_cost_inv / (implied_ncap_cost × capacity)
            if not cost_rows.empty and our_crf > 1e-9:
                # The implied NCAP_COST already accounts for the CRF difference
                # So DRATE effect = gap not explained by NCAP_COST alone
                # Use: effective_crf / our_crf - 1
                total_driver = cost_rows["capacity_driver_current"].astype(float).sum()
                if total_driver > 1e-9 and ncap_cost_delta != 0:
                    # After NCAP_COST adjustment, remaining gap attributable to CRF
                    remaining_after_cost = total_gap - ncap_cost_delta
                    # Approximate: if NCAP_COST were correct, CRF difference explains rest
                    # effective_crf = actual / (NCAP_COST_input × capacity)
                    inp_total = (inp * weights).sum()
                    if inp_total > 1e-9:
                        eff_annual = total_actual
                        our_annual = total_expected
                        # CRF ratio = actual/expected after correcting for NCAP_COST
                        cost_corrected_expected = total_expected + ncap_cost_delta
                        if cost_corrected_expected > 1e-9:
                            crf_ratio = total_actual / cost_corrected_expected
                            drate_delta = cost_corrected_expected * (crf_ratio - 1.0) - (total_gap - ncap_cost_delta - (cost_corrected_expected * (crf_ratio - 1.0)))
                            # Simplified: attribute remaining gap minus what's already captured
                            drate_delta = remaining_after_cost * 0.0  # will be captured in residual
                # If no NCAP_COST data, entire gap could be DRATE-related
                if drate_pct is None and ncap_cost_delta == 0.0:
                    # Estimate: what if true rate is 8% vs our fallback G_DRATE?
                    crf_gdrate = calculate_crf(gdrate, elife_i)
                    crf_8pct = calculate_crf(0.08, elife_i)
                    if crf_gdrate > 1e-9:
                        drate_delta = total_expected * (crf_8pct / crf_gdrate - 1.0)
                        drate_delta = min(drate_delta, total_gap)  # cap at total gap
                        explained += drate_delta
                        steps.append((
                            "DRATE effect (estimated)",
                            drate_delta,
                            f"Using G_DRATE fallback ({gdrate*100:.1f}%). "
                            f"If true rate were 8%: ΔCost_Inv ≈ £{drate_delta:.2f}M"
                        ))

    # Step 4: ELIFE re-levelization
    elife_delta = 0.0
    if ref_data is not None:
        elife = profile.get("elife_years")
        tlife = profile.get("tlife_years")
        if elife is not None and tlife is not None and int(elife) != int(tlife):
            elife_i = int(elife)
            tlife_i = int(tlife)
            crf_g_e = calculate_crf(gdrate, elife_i)
            crf_g_t = calculate_crf(gdrate, tlife_i)
            relevel = (crf_g_t / crf_g_e) if crf_g_e > 1e-15 else 1.0
            # This is already baked into our expected calculation, so it's
            # an explanatory note rather than an additional delta
            elife_delta = 0.0  # already included in expected
            steps.append((
                "Re-levelization (ELIFE≠TLIFE)",
                elife_delta,
                f"ELIFE={elife_i}yr, TLIFE={tlife_i}yr → relevel factor "
                f"= {relevel:.4f}. Already included in 'Expected'."
            ))

    # Step 5: IDC effect (Case 2)
    idc_delta = 0.0
    inv_case = recon_df["investment_case"].mode()
    inv_case = inv_case.iloc[0] if not inv_case.empty else "unknown"
    has_iled = bool(recon_df["has_nonzero_iled"].any()) if "has_nonzero_iled" in recon_df else False

    if inv_case in ("case_2a", "case_2b") and has_iled and ref_data is not None:
        iled = profile.get("iled_years", 0)
        iled_abs = abs(float(iled)) if iled is not None else 0.0
        if iled_abs > 0:
            idc_factor = (1.0 + gdrate) ** (iled_abs / 2.0)
            # IDC is already in our expected calc (compute_vintage_annuity includes it).
            # But if our IDC approximation differs from TIMES' exact calculation:
            idc_delta = 0.0  # already in expected
            steps.append((
                "IDC factor (Case 2)",
                idc_delta,
                f"|ILED|={iled_abs:.0f}yr → IDC = (1+{gdrate:.3f})^({iled_abs:.0f}/2) "
                f"= {idc_factor:.4f}. Already included in 'Expected'. "
                f"TIMES uses exact construction cash-flow timing which may differ ±2%."
            ))

    # Step 6: Residual
    residual = total_gap - explained
    steps.append((
        "Residual (unexplained)",
        residual,
        f"£{residual:.2f}M ({residual/total_expected*100:.1f}% of expected). "
        "Possible causes: TIMES internal interpolation, solver tolerances, "
        "OBLONG boundary effects, progressive spread within D(t) years."
    ))

    # Final: Actual value
    steps.append(("Actual (solver Cost_Inv)", total_actual,
                  f"VD reported total: £{total_actual:.2f}M"))

    return steps
