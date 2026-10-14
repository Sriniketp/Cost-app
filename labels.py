"""Authoritative, live UK TIMES process and commodity labels.

The labels are maintained in the ``uktm-lookup-tables`` repository and
published to the results SQL database (``usr.process_labels`` and
``usr.commodity_labels``).  The app reads them from SQL only.  It never falls
back to the legacy merged lookup tables, so missing or malformed live data is
reported to the caller instead of silently changing taxonomy.
"""
from __future__ import annotations

import pandas as pd

from cost_allocation.sql_connection import SqlSettings, SqlSourceError, fetch_dataframe


PROCESS_LABELS_TABLE = "usr.process_labels"
COMMODITY_LABELS_TABLE = "usr.commodity_labels"

# Fixed identifiers, never user input; quoted for SQL Server.
_TABLE_SQL = {
    PROCESS_LABELS_TABLE: "[usr].[process_labels]",
    COMMODITY_LABELS_TABLE: "[usr].[commodity_labels]",
}

PROCESS_LABEL_COLUMNS = (
    "process", "process_name", "process_group", "process_category",
    "process_subsector", "process_sector", "process_ghgi", "process_dukes",
    "process_useful_output", "in_current_model", "activity_unit",
    "capacity_unit", "process_set",
)
COMMODITY_LABEL_COLUMNS = (
    "commodity", "commodity_name", "commodity_subgroup", "commodity_group",
    "commodity_function", "commodity_type", "unit", "commodity_set",
)


class LabelSourceError(ValueError):
    """Raised when a live label source cannot be used safely."""


def label_source_fingerprints(
    settings: SqlSettings | None = None,
) -> tuple[tuple[str, int, int], ...]:
    """Return ``(source, row_count, checksum)`` for the process and commodity tables.

    Used as a cache key: a republished label table changes its row count or
    content checksum, which invalidates cached frames and session results.
    """
    configured = settings or SqlSettings.from_environment()
    query = " UNION ALL ".join(
        f"SELECT ? AS label_table, COUNT_BIG(*) AS row_count, "
        f"CHECKSUM_AGG(BINARY_CHECKSUM(*)) AS content_checksum FROM {_TABLE_SQL[table]}"
        for table in (PROCESS_LABELS_TABLE, COMMODITY_LABELS_TABLE)
    )
    try:
        frame = fetch_dataframe(
            query, (PROCESS_LABELS_TABLE, COMMODITY_LABELS_TABLE), settings=configured
        )
    except SqlSourceError as exc:
        raise LabelSourceError(f"Could not read authoritative label tables: {exc}") from exc

    source = "|".join(configured.cache_identity())
    by_table = {str(row.label_table): row for row in frame.itertuples(index=False)}
    fingerprints = []
    for table in (PROCESS_LABELS_TABLE, COMMODITY_LABELS_TABLE):
        row = by_table.get(table)
        if row is None:
            raise LabelSourceError(f"Authoritative label table {table} returned no metadata")
        checksum = 0 if pd.isna(row.content_checksum) else int(row.content_checksum)
        fingerprints.append((f"{source}:{table}", int(row.row_count), checksum))
    return tuple(fingerprints)


def validate_labels(
    labels: pd.DataFrame, required_columns: tuple[str, ...], key: str
) -> pd.DataFrame:
    """Validate a raw label frame and add the normalised ``_label_key`` column."""
    missing = set(required_columns).difference(labels.columns)
    if missing:
        raise LabelSourceError(
            f"Authoritative {key} labels are missing required columns: {', '.join(sorted(missing))}"
        )

    labels = labels.loc[:, list(required_columns)].copy()
    for column in labels.columns:
        labels[column] = labels[column].fillna("").astype(str).str.strip()
        labels.loc[labels[column].eq("nan"), column] = ""

    labels["_label_key"] = labels[key].str.upper()
    blank = labels["_label_key"].eq("")
    duplicate = labels["_label_key"].duplicated(keep=False) & ~blank
    if blank.any() or duplicate.any():
        problems = []
        if blank.any():
            problems.append(f"{blank.sum()} blank {key} code(s)")
        if duplicate.any():
            problems.append(f"duplicate {key} code(s): {', '.join(labels.loc[duplicate, key].head(5))}")
        raise LabelSourceError(f"Invalid authoritative {key} labels ({'; '.join(problems)})")
    return labels.reset_index(drop=True)


def _load_labels(
    table: str,
    required_columns: tuple[str, ...],
    key: str,
    settings: SqlSettings | None,
) -> pd.DataFrame:
    try:
        raw = fetch_dataframe(f"SELECT * FROM {_TABLE_SQL[table]}", (), settings=settings)
    except SqlSourceError as exc:
        raise LabelSourceError(f"Could not read authoritative {key} labels from {table}: {exc}") from exc
    if raw.empty:
        raise LabelSourceError(f"Authoritative {key} label table {table} is empty")
    return validate_labels(raw, required_columns, key)


def load_process_labels(settings: SqlSettings | None = None) -> pd.DataFrame:
    """Load and validate the complete authoritative process taxonomy from SQL."""
    return _load_labels(PROCESS_LABELS_TABLE, PROCESS_LABEL_COLUMNS, "process", settings)


def load_commodity_labels(settings: SqlSettings | None = None) -> pd.DataFrame:
    """Load and validate the complete authoritative commodity taxonomy from SQL."""
    return _load_labels(COMMODITY_LABELS_TABLE, COMMODITY_LABEL_COLUMNS, "commodity", settings)


def process_label_coverage(processes: pd.Series, labels: pd.DataFrame) -> pd.DataFrame:
    """Return unmatched non-empty process codes for explicit UI reporting."""
    codes = processes.dropna().astype(str).str.strip()
    codes = codes[codes.ne("")].str.upper().drop_duplicates()
    known = set(labels.get("_label_key", pd.Series(dtype=str)))
    return pd.DataFrame({"process": sorted(set(codes).difference(known))})


def commodity_label_coverage(commodities: pd.Series, labels: pd.DataFrame) -> pd.DataFrame:
    """Return unmatched non-empty commodity codes for explicit UI reporting."""
    codes = commodities.dropna().astype(str).str.strip()
    codes = codes[codes.ne("")].str.upper().drop_duplicates()
    known = set(labels.get("_label_key", pd.Series(dtype=str)))
    return pd.DataFrame({"commodity": sorted(set(codes).difference(known))})
