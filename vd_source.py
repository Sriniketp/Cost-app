"""Source identity and provenance for canonical VD result rows.

This module contains no database or Streamlit dependency. SQL and offline file
readers can use the same immutable metadata without leaking
credentials or connection strings into the calculation pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


VD_QUERY_CONTRACT_VERSION = "vd-canonical-v1"
VD_SOURCE_TYPES = frozenset({"file", "sql"})


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"VD source {field_name} must not be blank.")
    return text


def _optional_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class VdSourceIdentity:
    """Immutable identity of the evidence selected from one VD source.

    ``source_id`` is a safe logical name, never a credential-bearing database
    connection string. ``source_version`` must change whenever the selected
    evidence changes. SQL readers must obtain it from an immutable database
    snapshot/import identifier rather than inventing it in the application.
    """

    source_type: str
    source_id: str
    source_version: str
    schema_version: str
    query_contract_version: str = VD_QUERY_CONTRACT_VERSION
    run_id: str | None = None
    scenario_id: str | None = None
    selected_sow: str | None = None

    def __post_init__(self) -> None:
        source_type = _required_text(self.source_type, "type").casefold()
        if source_type not in VD_SOURCE_TYPES:
            supported = ", ".join(sorted(VD_SOURCE_TYPES))
            raise ValueError(
                f"Unsupported VD source type {self.source_type!r}; expected one of: {supported}."
            )
        object.__setattr__(self, "source_type", source_type)
        for field_name in (
            "source_id", "source_version", "schema_version", "query_contract_version"
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name.replace("_", " ")),
            )
        for field_name in ("run_id", "scenario_id", "selected_sow"):
            object.__setattr__(
                self, field_name, _optional_text(getattr(self, field_name))
            )

    def cache_key(self) -> tuple[str, ...]:
        """Return the complete non-secret identity required for safe caching."""
        return (
            self.source_type,
            self.source_id,
            self.run_id or "",
            self.scenario_id or "",
            self.selected_sow or "",
            self.source_version,
            self.schema_version,
            self.query_contract_version,
        )


@dataclass(frozen=True)
class VdSourceProvenance:
    """Auditable metadata recorded after canonical VD rows are retrieved."""

    identity: VdSourceIdentity
    extracted_at: datetime
    row_count: int
    canonical_multiset_hash: str
    imported_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.extracted_at.tzinfo is None:
            raise ValueError("VD source extraction timestamp must include a timezone.")
        if self.imported_at is not None and self.imported_at.tzinfo is None:
            raise ValueError("VD source import timestamp must include a timezone.")
        if self.row_count < 0:
            raise ValueError("VD source row count must be non-negative.")
        object.__setattr__(
            self,
            "canonical_multiset_hash",
            _required_text(self.canonical_multiset_hash, "canonical multiset hash"),
        )
