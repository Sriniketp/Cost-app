"""Strict SQL source adapter for raw UK-TIMES VD result rows."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from cost_allocation.ingest import canonical_vd_multiset_hash, canonicalise_vd_frame
from cost_allocation.sql_connection import (
    SqlSettings,
    SqlSourceError,
    fetch_dataframe,
)
from cost_allocation.vd_source import (
    VD_QUERY_CONTRACT_VERSION,
    VdSourceIdentity,
    VdSourceProvenance,
)

RAW_VD_QUERY = "EXEC usr.Get_RawVD @RunList = ?"
RAW_VD_SCHEMA_VERSION = "usr.Get_RawVD:v1"
RAW_VD_COLUMNS = (
    "run_id",
    "year",
    "attribute",
    "commodity",
    "process",
    "pv",
    "timeslice",
    "region",
    "vintage",
    "user_constraint",
)


@dataclass(frozen=True)
class VdSqlSnapshot:
    """Database-owned immutable identity resolved before result rows are read."""

    source_id: str
    run_id: str
    scenario_id: str
    source_version: str
    schema_version: str
    imported_at: datetime | None = None
    query_contract_version: str = VD_QUERY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for field_name in ("run_id", "scenario_id"):
            if not str(getattr(self, field_name) or "").strip():
                raise ValueError(
                    f"VD SQL snapshot {field_name.replace('_', ' ')} must not be blank."
                )
        # Reuse the source identity's strict validation without retaining an
        # identity that lacks the selected SOW.
        identity = VdSourceIdentity(
            source_type="sql",
            source_id=self.source_id,
            run_id=self.run_id,
            scenario_id=self.scenario_id,
            selected_sow=None,
            source_version=self.source_version,
            schema_version=self.schema_version,
            query_contract_version=self.query_contract_version,
        )
        for field_name in (
            "source_id", "run_id", "scenario_id", "source_version",
            "schema_version", "query_contract_version",
        ):
            object.__setattr__(self, field_name, getattr(identity, field_name))
        if self.imported_at is not None and self.imported_at.tzinfo is None:
            raise ValueError("VD SQL import timestamp must include a timezone.")

    def source_identity(self, selected_sow: str | None) -> VdSourceIdentity:
        """Return the complete cache/provenance identity after SOW selection."""
        return VdSourceIdentity(
            source_type="sql",
            source_id=self.source_id,
            run_id=self.run_id,
            scenario_id=self.scenario_id,
            selected_sow=selected_sow,
            source_version=self.source_version,
            schema_version=self.schema_version,
            query_contract_version=self.query_contract_version,
        )


def _selected_sow(frame: pd.DataFrame) -> str | None:
    selected = frame["Sow"].dropna().astype(str).unique().tolist()
    if len(selected) > 1:
        raise ValueError(
            "Canonical SQL VD data contains more than one selected SOW: "
            + ", ".join(sorted(selected))
            + "."
        )
    return selected[0] if selected else None


def prepare_sql_vd_rows(
    rows: pd.DataFrame,
    snapshot: VdSqlSnapshot,
    *,
    sow: str | int | None = None,
    extracted_at: datetime | None = None,
) -> tuple[pd.DataFrame, VdSourceProvenance]:
    """Canonicalise captured SQL rows and attach immutable provenance.

    ``rows`` must already use the canonical column names required by the
    approved SQL mapping. The database reader performs a parameterized query
    for one ``snapshot`` and calls this function; it must
    not normalize, aggregate, deduplicate, or select SOWs independently.
    """
    canonical = canonicalise_vd_frame(
        rows,
        scenario=snapshot.scenario_id,
        sow=sow,
        source_name=(
            f"SQL snapshot {snapshot.source_id}/{snapshot.run_id}/"
            f"{snapshot.source_version}"
        ),
    )
    selected_sow = _selected_sow(canonical)
    provenance = VdSourceProvenance(
        identity=snapshot.source_identity(selected_sow),
        imported_at=snapshot.imported_at,
        extracted_at=extracted_at or datetime.now(UTC),
        row_count=len(canonical),
        canonical_multiset_hash=canonical_vd_multiset_hash(canonical),
    )
    return canonical, provenance


def _map_raw_vd_rows(rows: pd.DataFrame, requested_run_id: str) -> pd.DataFrame:
    """Validate the stored-procedure contract and map it to canonical names."""
    casefolded = [str(column).casefold() for column in rows.columns]
    if len(casefolded) != len(set(casefolded)):
        raise SqlSourceError("Raw VD SQL results contain duplicate column names.")
    actual = set(casefolded)
    expected = set(RAW_VD_COLUMNS)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        raise SqlSourceError(
            "Raw VD SQL schema does not match the approved contract ("
            + "; ".join(details)
            + ")."
        )

    normalized = rows.copy()
    normalized.columns = casefolded
    returned_run_ids = set(
        normalized["run_id"].dropna().astype(str).str.strip().unique()
    )
    if returned_run_ids != {requested_run_id}:
        returned = ", ".join(sorted(returned_run_ids)) or "none"
        raise SqlSourceError(
            f"Raw VD SQL returned run identity {returned!r}; "
            f"expected {requested_run_id!r}."
        )

    def dimension(column: str, *extra_nulls: str) -> pd.Series:
        values = normalized[column].copy()
        text = values.astype("string").str.strip().str.casefold()
        return values.mask(text.isin({"none", *extra_nulls}))

    return pd.DataFrame(
        {
            "Attribute": normalized["attribute"],
            "Sow": None,
            "Commodity": dimension("commodity"),
            "Process": dimension("process"),
            "Period": dimension("year", "all_years"),
            "Region": dimension("region"),
            "Vintage": dimension("vintage"),
            "TimeSlice": dimension("timeslice"),
            "UserConstraint": dimension("user_constraint"),
            "Value": normalized["pv"],
            "Scenario": normalized["run_id"],
        }
    )


def load_sql_vd(
    run_id: str,
    *,
    settings: SqlSettings | None = None,
) -> tuple[pd.DataFrame, VdSourceProvenance]:
    """Load and canonicalise one immutable run from ``usr.Get_RawVD``."""
    requested_run_id = str(run_id or "").strip()
    if not requested_run_id:
        raise ValueError("SQL VD run ID must not be blank.")
    configured = settings or SqlSettings.from_environment()
    rows = fetch_dataframe(
        RAW_VD_QUERY,
        (requested_run_id,),
        settings=configured,
    )
    if rows.empty:
        raise SqlSourceError(
            f"No raw VD rows were returned for run {requested_run_id!r}."
        )
    mapped = _map_raw_vd_rows(rows, requested_run_id)
    snapshot = VdSqlSnapshot(
        source_id=configured.source_id,
        run_id=requested_run_id,
        scenario_id=requested_run_id,
        source_version=requested_run_id,
        schema_version=RAW_VD_SCHEMA_VERSION,
    )
    return prepare_sql_vd_rows(mapped, snapshot)
