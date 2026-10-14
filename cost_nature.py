"""Classify reported process costs by economic nature: fuel imports vs capital.

Every model-reported cost row (``Cost_Inv``, ``Cost_Fom``, ``Cost_Act``,
``Cost_Flo``) is assigned to exactly one *bucket* using the reviewed rules in
``tools/lookup_tables/cost_nature_rules.csv``.  Rules are tried in ascending
``priority`` order and the first match wins, so a row can never be counted
twice.  A rule whose ``action`` is ``exclude`` removes its rows from every
total but keeps them in the evidence and the reconciliation.  A row matched by
no rule is kept as ``Unclassified`` rather than dropped, so
shown + excluded + unclassified always equals the input rows.

This is a re-labelling of the rows the Overview System view already sums.  It
does not allocate, share, re-price or re-discount anything: scenarios such as
fossil price shocks come from the model runs themselves.

Pure domain module: must not import Streamlit.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import translate
from pathlib import Path

import pandas as pd

from cost_allocation.text_norm import normalise_code

DEFAULT_COST_NATURE_RULES_PATH = (
    Path(__file__).parent.parent / "lookup_tables" / "cost_nature_rules.csv"
)

RULE_COLUMNS = (
    "priority",
    "action",
    "attributes",
    "process",
    "process_subsector",
    "process_group",
    "energy_role",
    "bucket",
    "headline",
)
MATCH_FIELDS = ("process_subsector", "process_group", "energy_role")
SUPPORTED_ATTRIBUTES = frozenset({"Cost_Inv", "Cost_Fom", "Cost_Act", "Cost_Flo"})
ENERGY_ROLES = frozenset({"end_use", "supply", "other"})
ACTIONS = frozenset({"include", "exclude"})
WILDCARD = "*"
UNCLASSIFIED = "Unclassified"
ENERGY_ROLE_COLUMN = "energy_role"
RECONCILIATION_TOLERANCE_M = 1e-6  # £M; buckets are a partition, so any gap is a defect.

_CLASSIFIED_COLUMNS = ("bucket", "headline", "cost_nature_rule", "excluded")
_SUMMARY_COLUMNS = ["Run", "Period", "bucket", "headline", "Value"]


@dataclass(frozen=True)
class CostNatureRules:
    """Validated, priority-ordered rule table with its display orders.

    The display orders cover ``include`` rules only; excluded buckets never
    appear in a chart.
    """

    table: pd.DataFrame
    bucket_order: tuple[str, ...]
    headline_order: tuple[str, ...]


def cost_nature_rules_fingerprint(
    path: Path | str = DEFAULT_COST_NATURE_RULES_PATH,
) -> tuple[str, int, int]:
    """Return source metadata so caches invalidate when the reviewed rules change."""
    source = Path(path)
    try:
        stat = source.stat()
        return (str(source), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return (str(source), -1, -1)


def _split_alternatives(value: str) -> frozenset[str] | None:
    """Return case-folded alternatives, or ``None`` for the wildcard."""
    text = str(value).strip()
    if text == WILDCARD:
        return None
    parts = {part.strip().casefold() for part in text.split("|") if part.strip()}
    if not parts:
        raise ValueError(f"Empty match value {value!r}; use {WILDCARD!r} for 'any'.")
    return frozenset(parts)


def _process_pattern(value: str) -> re.Pattern[str] | None:
    """Compile ``|``-separated glob patterns (e.g. ``IMP*-E``); ``None`` for the wildcard."""
    text = str(value).strip()
    if text == WILDCARD:
        return None
    parts = [part.strip().upper() for part in text.split("|") if part.strip()]
    if not parts:
        raise ValueError(f"Empty process pattern {value!r}; use {WILDCARD!r} for 'any'.")
    return re.compile("|".join(f"(?:{translate(part)})" for part in parts))


def validate_cost_nature_rules(raw: pd.DataFrame) -> CostNatureRules:
    """Validate a raw rule table and return it in priority order.

    Raises ``ValueError`` on any structural problem so a bad edit to the lookup
    table fails loudly instead of silently moving costs between buckets.
    """
    missing = [column for column in RULE_COLUMNS if column not in raw.columns]
    if missing:
        raise ValueError(f"Cost-nature rules are missing columns: {missing}")
    rules = raw.loc[:, list(RULE_COLUMNS)].copy()
    for column in RULE_COLUMNS:
        rules[column] = rules[column].fillna("").astype(str).str.strip()

    try:
        rules["priority"] = rules["priority"].astype(int)
    except ValueError as exc:
        raise ValueError("Cost-nature rule priorities must be integers.") from exc
    duplicated = rules.loc[rules["priority"].duplicated(), "priority"].tolist()
    if duplicated:
        raise ValueError(f"Duplicate cost-nature rule priorities: {duplicated}")

    for column in ("bucket", "headline", "attributes", "action", "process"):
        blank = rules.loc[rules[column].eq(""), "priority"].tolist()
        if blank:
            raise ValueError(f"Blank {column} in cost-nature rules with priority {blank}")
    bad_actions = sorted(set(rules["action"]) - ACTIONS)
    if bad_actions:
        raise ValueError(f"Unknown cost-nature rule action(s) {bad_actions}; allowed: {sorted(ACTIONS)}")
    if rules["bucket"].eq(UNCLASSIFIED).any():
        raise ValueError(f"{UNCLASSIFIED!r} is reserved and cannot be a rule bucket.")

    for priority, value in zip(rules["priority"], rules["attributes"]):
        attributes = {part.strip() for part in value.split("|") if part.strip()}
        unknown = attributes - SUPPORTED_ATTRIBUTES
        if unknown or not attributes:
            raise ValueError(
                f"Rule {priority} has unsupported attributes {sorted(unknown) or value!r}; "
                f"allowed: {sorted(SUPPORTED_ATTRIBUTES)}"
            )
    for priority, value in zip(rules["priority"], rules["energy_role"]):
        roles = _split_alternatives(value)
        if roles is not None and not roles <= ENERGY_ROLES:
            raise ValueError(
                f"Rule {priority} has unknown energy_role {value!r}; allowed: {sorted(ENERGY_ROLES)}"
            )
    for column in MATCH_FIELDS:
        for value in rules[column]:
            _split_alternatives(value)
    for value in rules["process"]:
        _process_pattern(value)

    for column in ("headline", "action"):
        per_bucket = rules.groupby("bucket")[column].nunique()
        inconsistent = per_bucket[per_bucket > 1].index.tolist()
        if inconsistent:
            raise ValueError(f"Buckets mapped to more than one {column}: {inconsistent}")

    rules = rules.sort_values("priority", kind="stable").reset_index(drop=True)
    shown = rules[rules["action"].eq("include")]
    bucket_order = tuple(dict.fromkeys(shown["bucket"]))
    headline_order = tuple(dict.fromkeys(shown["headline"]))
    return CostNatureRules(table=rules, bucket_order=bucket_order, headline_order=headline_order)


def load_cost_nature_rules(
    path: Path | str = DEFAULT_COST_NATURE_RULES_PATH,
) -> CostNatureRules:
    """Read and validate the reviewed cost-nature rule table."""
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    return validate_cost_nature_rules(raw)


def attach_energy_role(rows: pd.DataFrame, energy_scope) -> pd.DataFrame:
    """Return a copy of *rows* with ``energy_role`` from the run's VDT-derived scope.

    End-use membership wins over supply membership.  Without a scope every row
    is ``other``; investment then lands in ``Capital - other`` instead of being
    guessed.
    """
    result = rows.copy()
    if result.empty:
        result[ENERGY_ROLE_COLUMN] = pd.Series(dtype=str)
        return result
    result[ENERGY_ROLE_COLUMN] = "other"
    if energy_scope is None:
        return result
    codes = normalise_code(result["Process"])
    supply = {str(code).strip().upper() for code in energy_scope.supply_processes}
    end_use = {str(code).strip().upper() for code in energy_scope.end_use_processes}
    result.loc[codes.isin(supply), ENERGY_ROLE_COLUMN] = "supply"
    result.loc[codes.isin(end_use), ENERGY_ROLE_COLUMN] = "end_use"
    return result


def _field_mask(values: pd.Series, allowed: frozenset[str] | None) -> pd.Series:
    if allowed is None:
        return pd.Series(True, index=values.index)
    return values.isin(allowed)


def classify_cost_nature(rows: pd.DataFrame, rules: CostNatureRules) -> pd.DataFrame:
    """Return a copy of *rows* with one bucket per row (first matching rule wins).

    Requires ``Attribute``; label columns and ``energy_role`` are treated as
    blank when absent, so a blank-labelled row can only match wildcard rules.
    """
    result = rows.copy()
    dtypes = {"cost_nature_rule": "Int64", "excluded": bool}
    for column in _CLASSIFIED_COLUMNS:
        result[column] = pd.Series(dtype=dtypes.get(column, object))
    if result.empty:
        return result

    attribute = result["Attribute"].fillna("").astype(str).str.strip()
    process = (
        normalise_code(result["Process"])
        if "Process" in result.columns
        else pd.Series("", index=result.index)
    )
    fields = {
        column: (
            result[column].fillna("").astype(str).str.strip().str.casefold()
            if column in result.columns
            else pd.Series("", index=result.index)
        )
        for column in MATCH_FIELDS
    }
    unassigned = pd.Series(True, index=result.index)
    bucket = pd.Series(UNCLASSIFIED, index=result.index, dtype=object)
    headline = pd.Series(UNCLASSIFIED, index=result.index, dtype=object)
    rule_id = pd.Series(pd.NA, index=result.index, dtype="Int64")
    excluded = pd.Series(False, index=result.index, dtype=bool)

    for rule in rules.table.itertuples(index=False):
        attributes = {part.strip() for part in rule.attributes.split("|") if part.strip()}
        mask = unassigned & attribute.isin(attributes)
        for column in MATCH_FIELDS:
            mask &= _field_mask(fields[column], _split_alternatives(getattr(rule, column)))
        pattern = _process_pattern(rule.process)
        if pattern is not None:
            mask &= process.map(lambda code, p=pattern: bool(p.fullmatch(code))).astype(bool)
        if not mask.any():
            continue
        bucket.loc[mask] = rule.bucket
        headline.loc[mask] = rule.headline
        rule_id.loc[mask] = rule.priority
        excluded.loc[mask] = rule.action == "exclude"
        unassigned &= ~mask

    result["bucket"] = bucket
    result["headline"] = headline
    result["cost_nature_rule"] = rule_id
    result["excluded"] = excluded
    return result


def _shown(classified: pd.DataFrame) -> pd.DataFrame:
    if "excluded" not in classified.columns:
        return classified
    return classified[~classified["excluded"].fillna(False).astype(bool)]


def summarise_cost_nature(
    classified: pd.DataFrame,
    level: str = "bucket",
    *,
    excluded: bool = False,
) -> pd.DataFrame:
    """Sum classified rows by Run, Period and the requested level.

    ``level`` is ``"bucket"`` or ``"headline"``.  By default only
    shown rows are summed; ``excluded=True`` sums only the excluded rows (used
    for the memo line that proves shown + excluded = System total).
    """
    if level not in {"bucket", "headline"}:
        raise ValueError("level must be 'bucket' or 'headline'.")
    if excluded and not classified.empty:
        classified = classified[classified["excluded"].fillna(False).astype(bool)]
    elif not excluded:
        classified = _shown(classified)
    if classified.empty:
        return pd.DataFrame(columns=_SUMMARY_COLUMNS)
    frame = classified.copy()
    if "Run" not in frame.columns:
        frame["Run"] = ""
    frame["Value"] = pd.to_numeric(frame["Value"], errors="coerce").fillna(0.0).astype(float)
    keys = ["Run", "Period", "bucket", "headline"]
    grouped = frame.groupby(keys, as_index=False, dropna=False)["Value"].sum()
    if level == "headline":
        grouped = grouped.groupby(["Run", "Period", "headline"], as_index=False)["Value"].sum()
        grouped["bucket"] = grouped["headline"]
    return grouped.loc[:, _SUMMARY_COLUMNS]


def reconcile_cost_nature(source: pd.DataFrame, classified: pd.DataFrame) -> pd.DataFrame:
    """Prove shown + excluded buckets partition the source rows per Run and Period.

    ``classified_total_m`` is the shown (included, classified) total.
    ``difference_m = source - shown - excluded``; it equals the unclassified
    value, so any non-zero result names rows the rule table does not yet cover.
    """
    columns = [
        "Run", "Period", "source_rows", "classified_rows", "source_total_m",
        "classified_total_m", "excluded_rows", "excluded_m", "unclassified_rows",
        "unclassified_m", "difference_m", "status",
    ]
    if source.empty and classified.empty:
        return pd.DataFrame(columns=columns)

    def _totals(frame: pd.DataFrame, prefix: str) -> pd.DataFrame:
        frame = frame.copy()
        if "Run" not in frame.columns:
            frame["Run"] = ""
        frame["Value"] = pd.to_numeric(frame["Value"], errors="coerce").fillna(0.0).astype(float)
        return frame.groupby(["Run", "Period"], dropna=False).agg(
            **{f"{prefix}_rows": ("Value", "size"), f"{prefix}_total_m": ("Value", "sum")}
        )

    src = _totals(source, "source")
    is_excluded = classified["excluded"].fillna(False).astype(bool)
    assigned = classified[classified["bucket"].ne(UNCLASSIFIED) & ~is_excluded]
    cls = _totals(assigned, "classified")
    exc = _totals(classified[is_excluded], "excluded").rename(columns={"excluded_total_m": "excluded_m"})
    unc = _totals(classified[classified["bucket"].eq(UNCLASSIFIED)], "unclassified").rename(
        columns={"unclassified_total_m": "unclassified_m"}
    )
    every = _totals(classified, "all")
    out = src.join([cls, exc, unc, every], how="outer").fillna(0.0)
    out["classified_rows"] = out["all_rows"].astype(int)
    out = out.drop(columns=["all_rows", "all_total_m"])
    out["source_rows"] = out["source_rows"].astype(int)
    out["unclassified_rows"] = out["unclassified_rows"].astype(int)
    out["excluded_rows"] = out["excluded_rows"].astype(int)
    out["difference_m"] = out["source_total_m"] - out["classified_total_m"] - out["excluded_m"]
    ok = (
        out["source_rows"].eq(out["classified_rows"])
        & out["unclassified_rows"].eq(0)
        & out["difference_m"].abs().le(RECONCILIATION_TOLERANCE_M)
    )
    out["status"] = ok.map({True: "OK", False: "CHECK"})
    return out.reset_index().loc[:, columns]


def excluded_cost_check(classified: pd.DataFrame, tolerance_m: float = 1e-6) -> pd.DataFrame:
    """Return excluded rows that carry a non-zero cost, summed per Run, Period and Process.

    Excluded processes (the re-import loop: process subsector ``Fuel Re-imports``) are
    expected to carry
    no cost: domestic production is costed on the extraction processes. An empty result
    confirms that; any row returned is cost that this view leaves out of its totals and
    should be raised with the model team.
    """
    columns = ["Run", "Period", "Process", "bucket", "Value"]
    if classified.empty or "excluded" not in classified.columns:
        return pd.DataFrame(columns=columns)
    rows = classified[classified["excluded"].fillna(False).astype(bool)]
    if rows.empty:
        return pd.DataFrame(columns=columns)
    totals = rows.groupby(["Run", "Period", "Process", "bucket"], as_index=False, sort=True)["Value"].sum()
    return totals[totals["Value"].abs() > tolerance_m].reset_index(drop=True)


def cumulative_spend(
    summary: pd.DataFrame,
    period_durations: Mapping[str, Mapping[int, float]],
    default_duration: float = 5.0,
) -> pd.DataFrame:
    """Undiscounted spend over the modelled periods: sum of annual cost x period length.

    Matches the "Undiscounted spend" convention already used by the Overview
    System table (milestone-year annual cost times period duration).
    """
    columns = ["Run", "bucket", "headline", "Value"]
    if summary.empty:
        return pd.DataFrame(columns=columns)
    frame = summary.copy()
    frame["duration"] = [
        float(period_durations.get(run, {}).get(int(period), default_duration))
        for run, period in zip(frame["Run"], frame["Period"])
    ]
    frame["Value"] = frame["Value"].astype(float) * frame["duration"]
    return frame.groupby(["Run", "bucket", "headline"], as_index=False)["Value"].sum().loc[:, columns]
