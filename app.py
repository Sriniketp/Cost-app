"""
Streamlit dashboard for UK TIMES Cost Results — mirrors vistool pipeline.

Run with:
    streamlit run tools/cost_allocation/app.py
"""

from __future__ import annotations

import sys
import time
import hashlib
from pathlib import Path

# Use parent.parent WITHOUT .resolve() — on network-mapped drives (a drive letter →
# \\server\share), .resolve() converts to a UNC path that won't match the
# drive-letter entries already in sys.path, causing duplicate/conflicting
# package roots and stale-cache imports.
_pkg_dir = Path(__file__).parent.parent
_pkg_dir_str = str(_pkg_dir)
# Ensure local tools/ path wins even if another cost_allocation package is installed.
# Remove ALL forms (drive-letter and UNC) so only one copy stays at the front.
_to_remove = []
for _p in sys.path:
    try:
        if Path(_p).samefile(_pkg_dir):
            _to_remove.append(_p)
    except (OSError, ValueError):
        pass
for _p in _to_remove:
    sys.path.remove(_p)
sys.path.insert(0, _pkg_dir_str)

# Purge any previously-loaded cost_allocation package that came from a
# different location (e.g. an installed copy, or one loaded from the UNC path
# while we need the drive-letter version).
_this_pkg = str(Path(__file__).parent)
for _name in list(sys.modules.keys()):
    if _name == "cost_allocation" or _name.startswith("cost_allocation."):
        _mod = sys.modules[_name]
        _mod_file = str(getattr(_mod, "__file__", "") or "")
        if _mod_file and not _mod_file.startswith(_this_pkg):
            try:
                if not Path(_mod_file).parent.samefile(Path(__file__).parent):
                    del sys.modules[_name]
            except (OSError, ValueError):
                del sys.modules[_name]

# Flush import machinery caches so the freshly-set sys.path takes effect
# immediately (critical on Streamlit re-runs where path finders may be stale).
import importlib
importlib.invalidate_caches()

# Ensure the cost_allocation package itself is loaded from the correct
# location with the right __path__, so sub-module imports always resolve.
import cost_allocation as _ca_pkg
_ca_pkg.__path__ = [_this_pkg]
_ca_pkg.__file__ = str(Path(_this_pkg) / "__init__.py")

# Also ensure cost_allocation.tabs sub-package __path__ is correct.
_tabs_dir = str(Path(_this_pkg) / "tabs")
try:
    import cost_allocation.tabs as _tabs_pkg
    _tabs_pkg.__path__ = [_tabs_dir]
    _tabs_pkg.__file__ = str(Path(_tabs_dir) / "__init__.py")
except ImportError:
    pass

try:
    import streamlit as st
except ImportError:
    sys.exit("Streamlit not installed. Run: pip install streamlit")

import pandas as pd
import importlib.util



# ---------------------------------------------------------------------------
# Imports from extracted modules
# ---------------------------------------------------------------------------

from cost_allocation.run_bundle import (
    APP_BUILD, RunBundle, _run_colour,
)
from cost_allocation.data_cache import (
    cached_load_run_groups, cached_load_runlog, cached_load_sql_vd,
    cached_load_commodity_labels, cached_load_process_labels,
    cached_label_source_fingerprints, current_sql_cache_identity,
)
from cost_allocation.gdp_deflator import (
    cached_load_hmt_deflators_with_meta,
    get_price_base_factor,
)
from cost_allocation.constants import DEFAULT_PERIOD_DURATIONS
from cost_allocation.helpers import frame_fingerprint
from cost_allocation.labels import process_label_coverage
from cost_allocation.resource_rules import resource_pools_fingerprint
from cost_allocation.run_selector import (
    DdRepoConfigError,
    RunDdUnavailableError,
    parse_run_group_members,
)
from cost_allocation.sql_connection import SqlSourceError
from cost_allocation.vd_source import VD_QUERY_CONTRACT_VERSION, VdSourceProvenance
from cost_allocation.vd_sql import RAW_VD_SCHEMA_VERSION
from cost_allocation.workbook_jobs import cancel_all

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="UK TIMES Costs",
    page_icon="$",
    layout="wide",
    initial_sidebar_state="expanded",
)

_LOGO_PATH = (
    Path(__file__).parent.parent.parent
    / "Documentation"
    / "external-reference"
    / "UKTM logo"
    / "UKTM_Logo_main.png"
)


_SESSION_DEFAULTS = {
    "loaded_run_ids": [],
    "run_bundles_cache": {},
    "_run_bundle_cache_version": None,
    "_labels_fingerprint": None,
    "_resource_rules_fingerprint": None,
    "_ref_page": None,
    "_main_analysis_tab": "Overview",
    "_run_selector_mode": "Individual runs",
    "_resource_workbooks": {},
    "_resource_workbook_jobs": {},
}

_MAIN_ANALYSIS_TAB_LABELS = (
    "Run Selector", "Overview", "Technology Drill-Down",
    "Fuel Cost Attribution", "Resource & Fuel Pathways", "Methodology",
)
_MAIN_ANALYSIS_DEFAULT_TAB = "Overview"
_MAIN_ANALYSIS_TAB_RENAMES = {
    "End-use Costs": "Fuel Cost Attribution",
    "Fuel & Flow Costs": "Fuel Cost Attribution",
    "Fuel Supply-chain Costs": "Fuel Cost Attribution",
}


def _init_session_state() -> None:
    """Initialize app-owned session state in one predictable place."""
    for key, value in _SESSION_DEFAULTS.items():
        st.session_state.setdefault(key, value.copy() if isinstance(value, (list, dict)) else value)


_RUN_BUNDLE_CACHE_VERSION = "sql-only-v13"


def _invalidate_stale_run_bundle_cache() -> None:
    """Rebuild bundles when derived metadata changes between app versions."""
    if st.session_state.get("_run_bundle_cache_version") == _RUN_BUNDLE_CACHE_VERSION:
        return
    cancel_all(st.session_state.get("_resource_workbook_jobs"))
    st.session_state["run_bundles_cache"] = {}
    st.session_state.pop("_resource_allocation_results", None)
    st.session_state.pop("_resource_workbooks", None)
    st.session_state.pop("_resource_workbook_jobs", None)
    st.session_state.pop("_hydrogen_proof_workbooks", None)
    st.session_state["_run_bundle_cache_version"] = _RUN_BUNDLE_CACHE_VERSION


def _reset_loaded_run_state() -> None:
    """Clear all state derived from the current run selection."""
    cancel_all(st.session_state.get("_resource_workbook_jobs"))
    st.session_state["loaded_run_ids"] = []
    st.session_state["run_bundles_cache"] = {}
    for key in (
        "period_range", "_ref_page", "_main_analysis_tab",
        "_resource_allocation_results", "_resource_workbooks", "_resource_workbook_jobs",
        "_hydrogen_proof_workbooks", "_price_scaled_costs",
    ):
        st.session_state.pop(key, None)
    st.session_state["_main_analysis_tab"] = "Run Selector"


_PRICE_SCALED_COSTS_MAX = 8


def _price_scaled_costs(costs: pd.DataFrame, factor: float) -> pd.DataFrame:
    """Return *costs* re-expressed by a price-base *factor*, reused across reruns.

    Rebuilding the scaled frame on every rerun gave it a new identity each
    time, which defeated ``frame_fingerprint``'s memo and the allocation
    cache keyed on it. The key is the source content plus the exact factor, so
    a reloaded run or a new deflator release can never be served a stale frame.
    """
    cache = st.session_state.setdefault("_price_scaled_costs", {})
    key = (frame_fingerprint(costs), factor)
    scaled = cache.get(key)
    if scaled is not None:
        return scaled
    scaled = costs.copy()
    value_numeric = pd.to_numeric(scaled.get("Value"), errors="coerce")
    # Time_NPV is a discounting artefact, not a monetary amount in
    # a given year's prices, so it must be left unscaled.
    is_time_npv = scaled.get("Attribute").astype(str).eq("Time_NPV")
    scale_mask = value_numeric.notna() & (~is_time_npv)
    scaled.loc[scale_mask, "Value"] = value_numeric.loc[scale_mask] * factor
    while len(cache) >= _PRICE_SCALED_COSTS_MAX:
        cache.pop(next(iter(cache)))
    cache[key] = scaled
    return scaled


def _invalidate_changed_resource_pools() -> None:
    """Drop only results derived from a superseded curated pool sheet."""
    fingerprint = resource_pools_fingerprint()
    if st.session_state.get("_resource_rules_fingerprint") == fingerprint:
        return
    cancel_all(st.session_state.get("_resource_workbook_jobs"))
    for key in (
        "_resource_allocation_results", "_resource_workbooks", "_resource_workbook_jobs",
        "_hydrogen_proof_workbooks",
    ):
        st.session_state.pop(key, None)
    st.session_state["_resource_rules_fingerprint"] = fingerprint


def _main_analysis_tabs():
    """Return stateful main tabs without changing their widget identity.

    Streamlit includes ``default`` in a tab widget's identity. It must remain
    constant; otherwise selecting a new tab changes the identity during the
    selection rerun and can reset the browser to the old tab.
    """
    selected = st.session_state.get(
        "_main_analysis_tab", _MAIN_ANALYSIS_DEFAULT_TAB
    )
    selected = _MAIN_ANALYSIS_TAB_RENAMES.get(selected, selected)
    if selected not in _MAIN_ANALYSIS_TAB_LABELS:
        selected = _MAIN_ANALYSIS_DEFAULT_TAB
    st.session_state["_main_analysis_tab"] = selected
    return st.tabs(
        _MAIN_ANALYSIS_TAB_LABELS,
        key="_main_analysis_tab",
        default=_MAIN_ANALYSIS_DEFAULT_TAB,
        on_change="rerun",
    )


def _record_timing(stage: str, started_at: float) -> None:
    """Store a short, per-rerun timing for the built-in performance panel."""
    _record_duration(stage, time.perf_counter() - started_at)


def _record_duration(stage: str, seconds: float) -> None:
    """Store one measured duration for the built-in performance panel."""
    st.session_state.setdefault("_load_timings", []).append(
        (stage, seconds)
    )


# ---------------------------------------------------------------------------
# Data loading (cached) — delegated to data_cache.py
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# _load_run: load all data for one run into a RunBundle
# ---------------------------------------------------------------------------

def _load_run(
    run_id: str,
    run_dd_list: list[str] | None,
    run_date: pd.Timestamp | None,
    scenario: str,
    vd_df: pd.DataFrame,
    vd_source_provenance: VdSourceProvenance,
    process_labels: pd.DataFrame,
    commodity_labels: pd.DataFrame,
    colour: str = "#636EFA",
) -> RunBundle:
    """Load all data for a single run and return a RunBundle.

    Delegates to the core :func:`pipeline.run_pipeline` and wraps the
    result with UI-specific fields (colour, in-memory DD reference data,
    dd_dir/run_dd_list for diagnostics).
    """
    from cost_allocation.cost_backcalc import ReferenceData
    from cost_allocation.pipeline import run_pipeline
    from cost_allocation.run_config import RunConfig

    config = RunConfig(
        scenario=scenario,
        run_id=run_id,
        run_dd_list=run_dd_list,
        run_date=run_date,
        vd_source_provenance=vd_source_provenance,
    )
    started_at = time.perf_counter()
    result = run_pipeline(
        config,
        vd_df=vd_df,
        process_labels=process_labels,
        commodity_labels=commodity_labels,
    )
    _record_timing(f"Prepare {run_id}", started_at)
    for stage, seconds in result.pipeline_timings.items():
        _record_duration(f"{run_id}: {stage}", seconds)

    # Reference data (lifetimes, discount rates) for reconciliation is built
    # once, in memory, from the run's effective DD table.
    started_at = time.perf_counter()
    reference_data = (
        None if result.effective_dd_inputs.empty
        else ReferenceData.from_frame(result.effective_dd_inputs)
    )
    _record_timing(f"Build DD reference data for {run_id}", started_at)

    return RunBundle(
        _result=result,
        reference_data=reference_data,
        dd_dir=result.dd_dir,
        run_dd_list=result.run_dd_list,
        colour=colour,
    )


# ---------------------------------------------------------------------------
# Sidebar  (minimal — title, build stamp, loaded-run info, period range)
# ---------------------------------------------------------------------------

def sidebar() -> tuple[list[tuple[str, list | None]], pd.DataFrame, pd.DataFrame]:
    """Return SQL-backed run selections and the run catalogue.

    Run selection now happens in the main content area via a vistool-style
    interactive table. The sidebar shows the currently loaded runs.
    """
    st.sidebar.title("UK TIMES Costs")
    st.sidebar.caption(f"Build: {APP_BUILD}")

    sql_identity = current_sql_cache_identity()
    try:
        runlog = cached_load_runlog(sql_identity)
    except SqlSourceError as exc:
        st.sidebar.error("SQL RunLog unavailable")
        st.error(str(exc))
        st.stop()
    try:
        run_groups = cached_load_run_groups(sql_identity)
    except SqlSourceError as exc:
        run_groups = pd.DataFrame()
        st.sidebar.warning("SQL RunGroups unavailable")
        st.warning(f"Run groups could not be loaded: {exc}")

    run_selections: list[tuple[str, list | None]] = []

    if runlog.empty:
        st.sidebar.warning("The SQL RunLog returned no runs.")
        return (run_selections, runlog, run_groups)

    # If runs have been loaded via the main-area table, read them from session_state
    loaded_run_ids: list[str] = st.session_state.get("loaded_run_ids", [])
    if loaded_run_ids:
        st.sidebar.header("Loaded Runs")
        for rid in loaded_run_ids:
            st.sidebar.markdown(f"- **{rid}**")

    for rid in loaded_run_ids:
        run_selections.append((rid, None))

    # --- Price Base Year (GDP deflator conversion) ---
    # Shown unconditionally (even before any run is loaded) so the choice is
    # available from the very first screen, matching how other global
    # display options are surfaced. The selected year is read back out of
    # session_state in main() once run bundles exist, and used to rebase
    # each run's displayed costs from its own native price year.
    st.sidebar.header("Price Base Year")
    try:
        deflators_df, publication_month_year, publication_url = cached_load_hmt_deflators_with_meta()
        available_years = sorted(deflators_df["Year"].unique())
        # UK TIMES model costs are natively expressed in 2010 prices, so that
        # is the sensible default basis to load at and display.
        default_year = st.session_state.get("price_base_year", 2010 if 2010 in available_years else available_years[0])
        if default_year not in available_years:
            default_year = 2010 if 2010 in available_years else available_years[0]

        st.sidebar.selectbox(
            "Costs displayed in (year) prices",
            options=available_years,
            index=available_years.index(default_year),
            key="price_base_year",
            help=(
                "Costs are converted to this reference year using HMT GDP "
                "deflators. Applies to displayed cost tables and charts; "
                "reconciliation diagnostics remain in each run's native "
                "model price basis for validation purposes."
            ),
        )
        st.sidebar.caption(f"gov.uk GDP deflator publication used: {publication_month_year}")
        st.sidebar.caption(f"Source: {publication_url}")
    except Exception as exc:
        st.sidebar.warning(f"Could not load GDP deflators: {exc}")

    return (run_selections, runlog, run_groups)


# ---------------------------------------------------------------------------
# Run-selection table (main content area, vistool-style)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Run-selection table (main content area, vistool-style)
# ---------------------------------------------------------------------------
# Selection model
# ---------------
# The *selection* is an ordered list of run IDs stored in plain session state
# (``_rs_selected_ids``).  It is the single source of truth.  Widgets only
# display it and report edits against it, so it survives filtering, sorting,
# tab switches, sidebar changes, and the selector being hidden while analysis
# tabs are open.  Row positions are never used to identify a run.
_RS_SELECTED_KEY = "_rs_selected_ids"
_RS_EDITOR_NONCE_KEY = "_rs_editor_nonce"
_RS_GROUP_NONCE_KEY = "_rs_group_nonce"
_RS_GROUP_TICKS_KEY = "_rs_ticked_groups"
_RS_FILTER_SHADOW_KEY = "_rs_filter_values"
_RS_FILTER_WIDGETS = {
    # widget key: (display column, label, placeholder)
    "tbl_filt_run": ("Run ID", "Run ID", "Search run IDs"),
    "tbl_filt_desc": ("Description", "Description", "Search descriptions"),
    "tbl_filt_user": ("User", "User", "Search users"),
    "tbl_filt_ref": ("Ref ID", "Ref ID", "Search references"),
}
_RS_ONLY_SELECTED_KEY = "tbl_only_selected"
_RS_GROUP_FILTER_KEY = "tbl_filt_group"


def _prepare_run_selector_rows(
    runlog: pd.DataFrame,
    loaded_run_ids: list[str] | tuple[str, ...] = (),
) -> pd.DataFrame:
    """Return active runs in a stable order (latest CB7 dev pinned, then newest).

    Order deliberately does NOT depend on what is loaded or selected, so rows
    never jump around while the user is ticking them.
    """
    display_df = runlog[["run_id", "date", "user", "purpose", "objective", "ref_id"]].copy()
    display_df["run_id"] = display_df["run_id"].fillna("").astype(str).str.strip()
    display_df = display_df.loc[
        display_df["run_id"].ne("") & ~display_df["run_id"].str.startswith("#")
    ].drop_duplicates("run_id").copy()

    cb7_numbers = pd.to_numeric(
        display_df["run_id"].str.extract(r"(?i)^CB7_dev_(\d+)$", expand=False),
        errors="coerce",
    )
    latest_cb7 = cb7_numbers.max()
    is_latest_cb7 = (
        cb7_numbers.eq(latest_cb7)
        if pd.notna(latest_cb7)
        else pd.Series(False, index=display_df.index)
    )
    is_loaded = display_df["run_id"].isin(list(loaded_run_ids))
    display_df["Status"] = ""
    display_df.loc[is_loaded, "Status"] = "Loaded"
    display_df.loc[is_latest_cb7, "Status"] = "Latest CB7 dev"
    display_df.loc[is_latest_cb7 & is_loaded, "Status"] = "Latest CB7 dev · Loaded"
    display_df["_priority"] = (~is_latest_cb7).astype(int)
    return display_df.sort_values(
        ["_priority", "date", "run_id"],
        ascending=[True, False, False],
        na_position="last",
        kind="stable",
    ).drop(columns="_priority").reset_index(drop=True)


def _available_group_members(
    runs: object, available_run_ids: set[str]
) -> tuple[list[str], list[str]]:
    """Expand one RunGroups value into (active RunLog members, unmatched members)."""
    members = parse_run_group_members(runs)
    return (
        [run_id for run_id in members if run_id in available_run_ids],
        [run_id for run_id in members if run_id not in available_run_ids],
    )


def _filter_run_rows(
    display_df: pd.DataFrame,
    filters: dict[str, str],
    selected_ids: list[str],
    only_selected: bool,
) -> pd.DataFrame:
    """Apply literal (non-regex), case-insensitive substring filters."""
    mask = pd.Series(True, index=display_df.index)
    for column, needle in filters.items():
        needle = (needle or "").strip()
        if needle:
            mask &= display_df[column].astype(str).str.contains(
                needle, case=False, na=False, regex=False
            )
    if only_selected:
        mask &= display_df["Run ID"].isin(selected_ids)
    return display_df.loc[mask].reset_index(drop=True)


def _merge_visible_selection(
    current: list[str], visible_ids: list[str], ticked_ids: list[str]
) -> list[str]:
    """Update the selection from the visible rows only.

    Hidden (filtered-out) runs keep their state; visible runs are ticked or
    unticked exactly as shown.  Existing order is preserved and new runs are
    appended in display order.
    """
    visible = set(visible_ids)
    ticked = set(ticked_ids)
    kept = [rid for rid in current if rid not in visible or rid in ticked]
    added = [rid for rid in ticked_ids if rid not in set(kept)]
    return kept + added


def _rs_selected() -> list[str]:
    return list(st.session_state.get(_RS_SELECTED_KEY, []))


def _rs_set_selected(run_ids: list[str], *, refresh_editor: bool) -> None:
    st.session_state[_RS_SELECTED_KEY] = list(dict.fromkeys(run_ids))
    if refresh_editor:
        # A new editor key discards stale per-row edits so the table redraws
        # exactly from the stored selection.
        st.session_state[_RS_EDITOR_NONCE_KEY] = (
            st.session_state.get(_RS_EDITOR_NONCE_KEY, 0) + 1
        )


def _rs_add_runs(run_ids: list[str]) -> None:
    _rs_set_selected(_rs_selected() + list(run_ids), refresh_editor=True)


def _rs_remove_runs(run_ids: list[str]) -> None:
    drop = set(run_ids)
    _rs_set_selected([r for r in _rs_selected() if r not in drop], refresh_editor=True)


def _reconcile_group_ticks(
    ticked_codes: list[str],
    members_by_code: dict[str, list[str]],
    selected_ids: list[str],
) -> list[str]:
    """Keep only ticked groups whose runs are all still selected.

    Groups are never ticked automatically just because their runs happen to
    be selected (many groups share runs such as the central case); a tick is
    only dropped when the user removes one of the group's runs elsewhere.
    """
    selected = set(selected_ids)
    return [
        code for code in ticked_codes
        if members_by_code.get(code)
        and all(run_id in selected for run_id in members_by_code[code])
    ]


def _apply_group_ticks(
    current: list[str],
    members_by_code: dict[str, list[str]],
    ticked_before: list[str],
    ticked_after: list[str],
) -> list[str]:
    """Add runs of newly ticked groups and remove runs of newly unticked groups.

    A run shared with a group that is still ticked stays selected, and runs
    selected individually are only removed if they belong to an unticked group.
    """
    newly_on = [code for code in ticked_after if code not in ticked_before]
    newly_off = [code for code in ticked_before if code not in ticked_after]
    still_needed = {r for code in ticked_after for r in members_by_code.get(code, [])}
    removed = {
        r for code in newly_off for r in members_by_code.get(code, [])
    } - still_needed
    added = [r for code in newly_on for r in members_by_code.get(code, [])]
    kept = [run_id for run_id in current if run_id not in removed]
    return list(dict.fromkeys(kept + added))


def _rs_restore_filter_widgets() -> None:
    """Re-seed filter widgets that Streamlit discarded while they were hidden."""
    shadow = st.session_state.setdefault(_RS_FILTER_SHADOW_KEY, {})
    for key in (*_RS_FILTER_WIDGETS, _RS_GROUP_FILTER_KEY):
        if key not in st.session_state:
            st.session_state[key] = shadow.get(key, "")
    if _RS_ONLY_SELECTED_KEY not in st.session_state:
        st.session_state[_RS_ONLY_SELECTED_KEY] = bool(
            shadow.get(_RS_ONLY_SELECTED_KEY, False)
        )


def _rs_remember_filter(key: str, value) -> None:
    st.session_state.setdefault(_RS_FILTER_SHADOW_KEY, {})[key] = value


def _rs_clear_filters() -> None:
    for key in (*_RS_FILTER_WIDGETS, _RS_GROUP_FILTER_KEY):
        st.session_state[key] = ""
        _rs_remember_filter(key, "")
    st.session_state[_RS_ONLY_SELECTED_KEY] = False
    _rs_remember_filter(_RS_ONLY_SELECTED_KEY, False)


def _rs_load_selection() -> None:
    """Make the loaded runs exactly equal to the current selection."""
    selected = _rs_selected()
    if selected == list(st.session_state.get("loaded_run_ids", [])):
        return
    cancel_all(st.session_state.get("_resource_workbook_jobs"))
    st.session_state["loaded_run_ids"] = selected
    keep = set(selected)
    cache = st.session_state.get("run_bundles_cache", {})
    st.session_state["run_bundles_cache"] = {k: v for k, v in cache.items() if k in keep}
    for key in ("_resource_workbooks", "_resource_workbook_jobs", "period_range"):
        st.session_state.pop(key, None)
    st.session_state["_main_analysis_tab"] = "Overview"


@st.fragment
def _show_run_selector(runlog: pd.DataFrame, run_groups: pd.DataFrame) -> bool:
    """Render the run selector.  Returns True when runs are loaded."""
    loaded_ids: list[str] = list(st.session_state.get("loaded_run_ids", []))
    display_df = _prepare_run_selector_rows(runlog, loaded_ids).rename(columns={
        "run_id": "Run ID", "date": "Date", "user": "User",
        "purpose": "Description", "objective": "Objective (£bn)", "ref_id": "Ref ID",
    })
    display_df["Date"] = pd.to_datetime(display_df["Date"], errors="coerce")
    for column in ("User", "Description", "Ref ID"):
        display_df[column] = display_df[column].fillna("").astype(str)
    display_df["Objective (£bn)"] = pd.to_numeric(display_df["Objective (£bn)"], errors="coerce")
    display_df = display_df[
        ["Status", "Run ID", "Date", "User", "Description", "Objective (£bn)", "Ref ID"]
    ]
    active_run_ids = set(display_df["Run ID"])

    # First visit: start from what is loaded (empty on a fresh session — no
    # row is ever pre-selected).  Drop runs that have since been soft-deleted.
    if _RS_SELECTED_KEY not in st.session_state:
        st.session_state[_RS_SELECTED_KEY] = list(loaded_ids)
    selected_now = [r for r in _rs_selected() if r in active_run_ids]
    if selected_now != _rs_selected():
        _rs_set_selected(selected_now, refresh_editor=True)
    _rs_restore_filter_widgets()

    st.subheader("Choose your runs")
    st.caption(
        "Tick runs to select them. Filters only change what is shown — ticked runs "
        "stay selected while hidden. Ticking a saved group selects all of its runs."
    )
    selection_summary = st.empty()

    individual_tab, group_tab = st.tabs(
        ["Individual runs", "Saved run groups"],
        key="_run_selector_mode",
        default="Individual runs",
        on_change="rerun",
    )

    # ---------------- Individual runs ----------------
    with individual_tab:
        with st.container(border=True):
            st.markdown("**Filter individual runs**")
            cols = st.columns([1.4, 2.2, 1, 1])
            filters: dict[str, str] = {}
            for col, (key, (column, label, placeholder)) in zip(cols, _RS_FILTER_WIDGETS.items()):
                value = col.text_input(label, key=key, placeholder=placeholder)
                _rs_remember_filter(key, value)
                filters[column] = value
            only_selected = st.toggle("Show selected runs only", key=_RS_ONLY_SELECTED_KEY)
            _rs_remember_filter(_RS_ONLY_SELECTED_KEY, only_selected)

        filtered = _filter_run_rows(display_df, filters, _rs_selected(), only_selected)
        visible_ids = filtered["Run ID"].tolist()

        with st.container(horizontal=True):
            st.caption(
                f"Showing {len(filtered):,} of {len(display_df):,} active runs · "
                f"{len(_rs_selected()):,} selected"
            )
            st.button("Select all shown", icon=":material/done_all:",
                      on_click=_rs_add_runs, args=(visible_ids,),
                      disabled=not visible_ids, key="rs_select_shown")
            st.button("Unselect shown", icon=":material/remove_done:",
                      on_click=_rs_remove_runs, args=(visible_ids,),
                      disabled=not set(visible_ids) & set(_rs_selected()),
                      key="rs_unselect_shown")
            st.button("Clear filters", icon=":material/filter_alt_off:",
                      on_click=_rs_clear_filters, key="rs_clear_filters")

        editor_df = filtered.copy()
        editor_df.insert(0, "Select", editor_df["Run ID"].isin(_rs_selected()))
        view_hash = hashlib.sha1("\x1f".join(visible_ids).encode()).hexdigest()[:12]
        editor_key = (
            f"run_selector_editor_{view_hash}_"
            f"{st.session_state.get(_RS_EDITOR_NONCE_KEY, 0)}"
        )
        edited = st.data_editor(
            editor_df,
            key=editor_key,
            width="stretch",
            height=500,
            hide_index=True,
            num_rows="fixed",
            disabled=[c for c in editor_df.columns if c != "Select"],
            column_config={
                "Select": st.column_config.CheckboxColumn("✓", width="small", pinned=True),
                "Status": st.column_config.TextColumn(width="small"),
                "Run ID": st.column_config.TextColumn(width="medium", pinned=True),
                "Date": st.column_config.DateColumn(format="YYYY-MM-DD", width="small"),
                "User": st.column_config.TextColumn(width="small"),
                "Description": st.column_config.TextColumn(width="large"),
                "Objective (£bn)": st.column_config.NumberColumn(format="%.1f", width="small"),
                "Ref ID": st.column_config.TextColumn(width="small"),
            },
        )
        ticked = edited.loc[edited["Select"].fillna(False).astype(bool), "Run ID"].tolist()
        merged = _merge_visible_selection(_rs_selected(), visible_ids, ticked)
        if merged != _rs_selected():
            _rs_set_selected(merged, refresh_editor=False)
            if only_selected:
                # An unticked row should disappear from the "selected only" view now.
                st.rerun(scope="fragment")

    # ---------------- Saved run groups ----------------
    with group_tab:
        if run_groups.empty:
            st.info("No active SQL run groups are available.", icon=":material/info:")
        else:
            with st.container(border=True):
                st.markdown("**Filter saved groups**")
                group_filter = st.text_input(
                    "Group name, code, or description",
                    key=_RS_GROUP_FILTER_KEY,
                    placeholder="Search saved run groups",
                    label_visibility="collapsed",
                )
                _rs_remember_filter(_RS_GROUP_FILTER_KEY, group_filter)
            group_rows = run_groups.copy()
            resolved = group_rows["runs"].map(
                lambda runs: _available_group_members(runs, active_run_ids)
            )
            group_rows["_members"] = resolved.str[0]
            group_rows["_missing"] = resolved.str[1]
            group_rows["Run count"] = group_rows["_members"].map(len)
            members_by_code = dict(zip(group_rows["group_code"], group_rows["_members"]))
            ticked_codes = _reconcile_group_ticks(
                st.session_state.get(_RS_GROUP_TICKS_KEY, []),
                members_by_code,
                _rs_selected(),
            )
            st.session_state[_RS_GROUP_TICKS_KEY] = ticked_codes
            needle = group_filter.strip()
            if needle:
                group_mask = pd.Series(False, index=group_rows.index)
                for column in ("group_name", "group_code", "description"):
                    group_mask |= group_rows[column].str.contains(
                        needle, case=False, na=False, regex=False
                    )
                group_rows = group_rows.loc[group_mask]
            group_rows = group_rows.reset_index(drop=True)
            was_ticked = [code in ticked_codes for code in group_rows["group_code"]]
            group_display = group_rows.rename(columns={
                "group_code": "Code", "group_name": "Group", "description": "Description",
                "_members": "Runs",
            })[["Code", "Group", "Runs", "Run count", "Description"]]
            group_display.insert(0, "Select", was_ticked)
            st.caption(
                f"Showing {len(group_display):,} active groups · "
                "tick a group to select its runs, untick to remove them"
            )
            # The key changes whenever the visible groups or their ticked state
            # change, so the table always redraws from the stored selection.
            group_view_hash = hashlib.sha1("\x1f".join(
                f"{code}={ticked}" for code, ticked in zip(group_display["Code"], was_ticked)
            ).encode()).hexdigest()[:12]
            edited_groups = st.data_editor(
                group_display,
                key=f"run_group_editor_{group_view_hash}_"
                    f"{st.session_state.get(_RS_GROUP_NONCE_KEY, 0)}",
                width="stretch",
                height=400,
                hide_index=True,
                num_rows="fixed",
                disabled=[c for c in group_display.columns if c != "Select"],
                column_config={
                    "Select": st.column_config.CheckboxColumn("✓", width="small", pinned=True),
                    "Code": st.column_config.TextColumn(width="small", pinned=True),
                    "Group": st.column_config.TextColumn(width="medium"),
                    "Runs": st.column_config.ListColumn(width="large"),
                    "Run count": st.column_config.NumberColumn(format="%d", width="small"),
                    "Description": st.column_config.TextColumn(width="medium"),
                },
            )
            now_ticked = edited_groups["Select"].fillna(False).astype(bool).tolist()
            if now_ticked != was_ticked:
                visible_codes = group_rows["group_code"].tolist()
                # Hidden (filtered-out) ticked groups keep their tick.
                new_ticked_codes = [
                    code for code in ticked_codes if code not in visible_codes
                ] + [code for code, on in zip(visible_codes, now_ticked) if on]
                _rs_set_selected(
                    _apply_group_ticks(
                        _rs_selected(), members_by_code, ticked_codes, new_ticked_codes,
                    ),
                    refresh_editor=True,
                )
                st.session_state[_RS_GROUP_TICKS_KEY] = new_ticked_codes
                st.session_state[_RS_GROUP_NONCE_KEY] = (
                    st.session_state.get(_RS_GROUP_NONCE_KEY, 0) + 1
                )
                st.rerun(scope="fragment")
            group_missing = list(dict.fromkeys(
                rid
                for ticked, missing in zip(was_ticked, group_rows["_missing"])
                if ticked
                for rid in missing
            ))
            if group_missing:
                st.warning(
                    "Not in the active RunLog, so not selected: " + ", ".join(group_missing),
                    icon=":material/warning:",
                )

    # ---------------- Summary and actions ----------------
    selected_run_ids = _rs_selected()
    pending_change = selected_run_ids != loaded_ids
    with selection_summary.container():
        with st.container(border=True):
            col_sel, col_load = st.columns(2, vertical_alignment="top")
            with col_sel:
                st.caption("Selected")
                if selected_run_ids:
                    run_word = "run" if len(selected_run_ids) == 1 else "runs"
                    st.markdown(f"**{len(selected_run_ids):,} {run_word} selected**")
                    st.caption(", ".join(selected_run_ids))
                else:
                    st.markdown("**No runs selected**")
                    st.caption("Tick runs below or add a saved group.")
            with col_load:
                st.caption("Currently loaded")
                if loaded_ids:
                    run_word = "run" if len(loaded_ids) == 1 else "runs"
                    st.markdown(f"**{len(loaded_ids):,} {run_word} loaded**")
                    st.caption(", ".join(loaded_ids))
                else:
                    st.markdown("**No runs loaded**")
                    st.caption("Load a selection to begin analysis.")

            with st.container(horizontal=True, horizontal_alignment="right"):
                st.button("Clear selection", icon=":material/deselect:",
                          on_click=_rs_set_selected, args=([],),
                          kwargs={"refresh_editor": True},
                          disabled=not selected_run_ids, key="rs_clear_selection")
                clear_clicked = st.button("Unload all runs", icon=":material/clear_all:",
                                          disabled=not loaded_ids, key="rs_unload")
                load_clicked = st.button(
                    "Update loaded runs" if loaded_ids else "Load selected runs",
                    type="primary", icon=":material/play_arrow:",
                    disabled=not (selected_run_ids and pending_change),
                    key="rs_load",
                )

    if load_clicked:
        _rs_load_selection()
        st.rerun(scope="app")
    if clear_clicked:
        _reset_loaded_run_state()
        st.rerun(scope="app")

    return bool(loaded_ids)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _init_session_state()
    _invalidate_stale_run_bundle_cache()
    st.session_state["_load_timings"] = []

    (run_selections, runlog, run_groups) = sidebar()
    sql_identity = current_sql_cache_identity()

    _logo_col1, _logo_col2 = st.columns([1 , 40])
    with _logo_col2:
        st.image(str(_LOGO_PATH), width=120)

    has_loaded_runs = bool(st.session_state["loaded_run_ids"])

    # Stage 1: loading no run must be cheap. Do not create temporary files,
    # read lookup tables, resolve DD revisions, or render hidden analysis tabs.
    if not has_loaded_runs:
        if runlog.empty:
            st.error("The SQL RunLog returned no runs.")
        else:
            _show_run_selector(runlog, run_groups)
        return

    # Import analysis renderers only once the user has explicitly selected a
    # run. This keeps the initial selector shell independent of Plotly and the
    # heavier reconciliation/drill-down modules.
    from cost_allocation.tabs.drilldown import _show_drilldown
    from cost_allocation.tabs.methodology import _show_methodology
    from cost_allocation.tabs.mismatch import _show_mismatch_diagnosis
    from cost_allocation.tabs.overview_current import _show_overview
    from cost_allocation.tabs.reconciliation import _show_reconciliation
    from cost_allocation.tabs.resource_allocation import _show_resource_allocation

    started_at = time.perf_counter()
    try:
        label_fingerprints = cached_label_source_fingerprints(current_sql_cache_identity())
    except ValueError as exc:
        st.error(f"Authoritative labels are unavailable: {exc}")
        return
    if st.session_state.get("_labels_fingerprint") != label_fingerprints:
        cancel_all(st.session_state.get("_resource_workbook_jobs"))
        st.session_state["run_bundles_cache"] = {}
        st.session_state.pop("_resource_allocation_results", None)
        st.session_state.pop("_resource_workbooks", None)
        st.session_state.pop("_resource_workbook_jobs", None)
        st.session_state.pop("_hydrogen_proof_workbooks", None)
        st.session_state["_labels_fingerprint"] = label_fingerprints
    _invalidate_changed_resource_pools()
    try:
        process_labels = cached_load_process_labels(label_fingerprints[0])
        commodity_labels = cached_load_commodity_labels(label_fingerprints[1])
    except ValueError as exc:
        st.error(f"Authoritative labels are unavailable: {exc}")
        return
    _record_timing("Load authoritative labels", started_at)

    bundles: list[RunBundle] = []

    # --- SQL RunLog path (1+ runs from table selection) ---
    if run_selections:
        run_dates = runlog.set_index("run_id")["date"].to_dict()
        cached_bundles: dict[str, RunBundle] = st.session_state.get("run_bundles_cache", {})
        # Evict stale bundles that are missing columns added since they were cached
        _REQUIRED_COST_COLS = {"process_sector", "process_name"}
        cached_bundles = {
            k: v for k, v in cached_bundles.items()
            if (
                _REQUIRED_COST_COLS.issubset(v.costs.columns)
                and hasattr(v, "energy_scope")
                and hasattr(v, "annual_flows")
                and hasattr(v, "power_scope")
                and hasattr(v, "run_date")
                and hasattr(v, "missing_dd_files")
                and v.vd_source_provenance is not None
                and v.vd_source_provenance.identity.source_type == "sql"
                and v.vd_source_provenance.identity.source_id == sql_identity[0]
                and v.vd_source_provenance.identity.run_id == k
                and v.vd_source_provenance.identity.scenario_id == k
                and v.vd_source_provenance.identity.source_version == k
                and v.vd_source_provenance.identity.schema_version == RAW_VD_SCHEMA_VERSION
                and v.vd_source_provenance.identity.query_contract_version
                == VD_QUERY_CONTRACT_VERSION
            )
        }
        for idx, (rid, dd_list) in enumerate(run_selections):
            if rid in cached_bundles:
                b = cached_bundles[rid]
                b.colour = _run_colour(idx)
                bundles.append(b)
                continue
            started_at = time.perf_counter()
            try:
                vd_df_r, provenance = cached_load_sql_vd(rid, sql_identity)
            except (SqlSourceError, ValueError) as exc:
                st.error(f"Could not load SQL VD results for **{rid}**: {exc}")
                return
            _record_timing(f"Read SQL VD for {rid}", started_at)
            try:
                bundle = _load_run(
                    run_id=rid,
                    run_dd_list=dd_list,
                    run_date=run_dates.get(rid),
                    scenario=rid,
                    vd_df=vd_df_r,
                    vd_source_provenance=provenance,
                    process_labels=process_labels,
                    commodity_labels=commodity_labels,
                    colour=_run_colour(idx),
                )
            except DdRepoConfigError as exc:
                st.error(f"DD repository is not configured: {exc}")
                return
            except RunDdUnavailableError as exc:
                st.error(f"Could not load **{rid}**: {exc}")
                return
            cached_bundles[rid] = bundle
            bundles.append(bundle)
        st.session_state["run_bundles_cache"] = cached_bundles

    # --- Show DD/input status for each loaded bundle ---
    for b in bundles:
        has_dd = any(not v.empty for v in b.input_costs.values())
        if has_dd and b.run_dd_list:
            st.sidebar.caption(f"**{b.run_id}**: DD OK ({len(b.run_dd_list)} files)")
        elif not has_dd:
            st.sidebar.warning(f"**{b.run_id}**: DD inputs missing")
        if b.missing_dd_files:
            st.warning(
                f"**{b.run_id}**: {len(b.missing_dd_files)} DD file(s) listed in the "
                ".RUN file are missing at the run's uktm-dds commit: "
                + ", ".join(f"`{name}.dd`" for name in b.missing_dd_files)
            )
        if b.vdt_topology.empty:
            st.sidebar.warning(f"**{b.run_id}**: run-specific VDT topology missing or ambiguous")
        unmatched_processes = process_label_coverage(b.vd_df["Process"], process_labels)
        if not unmatched_processes.empty:
            st.sidebar.warning(
                f"**{b.run_id}**: {len(unmatched_processes)} process code(s) have no authoritative label"
            )
            with st.sidebar.expander(f"Unlabelled processes — {b.run_id}"):
                st.dataframe(unmatched_processes, hide_index=True, width="stretch")

    timings = st.session_state.get("_load_timings", [])
    if timings:
        with st.sidebar.expander("Latest load timings"):
            timing_df = pd.DataFrame(timings, columns=["Stage", "Seconds"])
            timing_df["Seconds"] = timing_df["Seconds"].round(2)
            st.dataframe(timing_df, hide_index=True, width="stretch")

    # --- Period range selector (universal) ---
    all_available_periods: set[int] = set()
    for b in bundles:
        all_available_periods.update(b.periods)
    all_periods_sorted = sorted(all_available_periods)

    if len(all_periods_sorted) >= 2:
        st.sidebar.header("Period Range")
        # Default: 2010–2050 (clamped to available data)
        default_lo = min(
            (p for p in all_periods_sorted if p >= 2010),
            default=all_periods_sorted[0],
        )
        default_hi = max(
            (p for p in all_periods_sorted if p <= 2050),
            default=all_periods_sorted[-1],
        )
        period_range = st.sidebar.select_slider(
            "Display range",
            options=all_periods_sorted,
            value=(default_lo, default_hi),
            key="period_range",
            help="Filter all charts and tables to this period range.",
        )
        p_lo, p_hi = period_range
        # Never mutate cached bundles: a later slider expansion must restore
        # every originally available period. Renderers receive lightweight
        # views with their period lists narrowed below.
    else:
        p_lo, p_hi = (
            (all_periods_sorted[0], all_periods_sorted[0])
            if all_periods_sorted else (2010, 2060)
        )

    # --- Apply price base year conversion (control lives in sidebar()) ---
    # Lets the user re-express every displayed cost in a chosen year's prices,
    # using the latest HMT GDP deflator publication from gov.uk. Each run's
    # own native price year (from its DD inputs, `b.price_year`) is used as
    # the conversion origin, so runs built on different price bases can still
    # be compared on a common footing.
    deflators_df = None
    target_price_year = st.session_state.get("price_base_year")
    if target_price_year is not None:
        try:
            deflators_df, _, _ = cached_load_hmt_deflators_with_meta()
        except Exception:
            deflators_df = None

    display_bundles: list[RunBundle] = []
    for b in bundles:
        view = RunBundle(
            _result=b,
            reference_data=b.reference_data,
            dd_dir=b.dd_dir,
            run_dd_list=b.run_dd_list,
            colour=b.colour,
        )
        view.periods = [p for p in b.periods if p_lo <= p <= p_hi]

        # --- Apply price base year conversion to this view only ---
        # Never mutate the cached bundle's `costs`: rebuild a scaled copy on
        # the lightweight view so switching the target year back, or
        # re-running with a different selection, always starts from the
        # run's original (unscaled) cost figures.
        #
        # UK TIMES VD outputs are natively expressed in 2010 prices. Prefer
        # the run's own DD-derived price year (`b.price_year`) when it is
        # known, but fall back to 2010 rather than skipping the conversion
        # entirely — DD inputs (and therefore `price_year`) are frequently
        # unavailable for SQL-loaded runs even though the underlying costs
        # are still in 2010 prices.
        origin_year = b.price_year if b.price_year is not None else 2010
        if (
            deflators_df is not None
            and target_price_year is not None
            and target_price_year != origin_year
        ):
            try:
                factor = get_price_base_factor(
                    deflators_df, from_year=origin_year, to_year=target_price_year,
                )
            except ValueError as exc:
                st.sidebar.warning(f"**{b.run_id}**: price base conversion failed: {exc}")
            else:
                view.costs = _price_scaled_costs(view.costs, factor)
                # Reflect the new basis in the price-year label shown throughout
                # the UI (axis units, captions, exports for this view).
                view.price_year = target_price_year

        display_bundles.append(view)
    bundles = display_bundles

    # --- Sidebar: diagnostics (full-page overlay, requires loaded runs) ---
    if bundles:
        st.sidebar.divider()
        st.sidebar.markdown("**Diagnostics**")
        if st.sidebar.button("Reconciliation", key="ref_reconciliation", width="stretch"):
            st.session_state["_ref_page"] = "reconciliation"
        if st.sidebar.button("Mismatch Diagnosis", key="ref_mismatch_diagnosis", width="stretch"):
            st.session_state["_ref_page"] = "mismatch_diagnosis"

    _active_ref = st.session_state.get("_ref_page")

    if _active_ref == "reconciliation" and bundles:
        st.button("← Back to analysis", key="ref_back_recon",
                  on_click=lambda: st.session_state.pop("_ref_page", None))
        _show_reconciliation(bundles)
        return
    elif _active_ref == "mismatch_diagnosis" and bundles:
        st.button("← Back to analysis", key="ref_back_diag",
                  on_click=lambda: st.session_state.pop("_ref_page", None))
        _show_mismatch_diagnosis(bundles)
        return

    # Dynamic tabs prevent hidden analysis bodies from constructing charts,
    # pivots, and diagnostics during an unrelated interaction.
    (
        tab_selector,
        tab_overview,
        tab_drill,
        tab_fuel_cost_attribution,
        tab_resource_pathways,
        tab_method,
    ) = _main_analysis_tabs()

    if tab_selector.open:
        with tab_selector:
            if runlog.empty:
                st.error("The SQL RunLog returned no runs.")
            else:
                _show_run_selector(runlog, run_groups)

    if tab_overview.open:
        with tab_overview:
            if bundles:
                _show_overview(bundles)
            else:
                st.info("No selected run could be loaded. Return to **Run Selector**.")

    if tab_drill.open:
        with tab_drill:
            if bundles:
                _show_drilldown(bundles)
            else:
                st.info("No selected run could be loaded. Return to **Run Selector**.")

    if tab_fuel_cost_attribution.open:
        with tab_fuel_cost_attribution:
            if bundles:
                _show_resource_allocation(bundles, page="attribution")
            else:
                st.info("No selected run could be loaded. Return to **Run Selector**.")

    if tab_resource_pathways.open:
        with tab_resource_pathways:
            if bundles:
                _show_resource_allocation(bundles, page="pathways")
            else:
                st.info("No selected run could be loaded. Return to **Run Selector**.")

    if tab_method.open:
        with tab_method:
            pd_to_use = bundles[0].period_durations if bundles else dict(DEFAULT_PERIOD_DURATIONS)
            _show_methodology(bundles, fallback_period_durations=pd_to_use)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
