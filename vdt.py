"""Read structural TIMES VDT commodity-flow definitions.

VDT records describe model topology; observed quantities remain sourced from
the selected run's ``VAR_FIn`` and ``VAR_FOut`` rows in its VD results.
"""
from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path

import pandas as pd


VDT_COLUMNS = ("region", "process", "commodity", "direction")
_DIRECTIONS = {"IN", "OUT"}


class VdtParseError(ValueError):
    """Raised when a VDT file cannot provide valid flow records."""


def parse_vdt_text(text: str) -> pd.DataFrame:
    """Parse quoted VEDA VDT records while ignoring directives and comments."""
    rows: list[tuple[str, str, str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("*", "$", "#")) or not line.startswith('"'):
            continue
        try:
            values = next(csv.reader(StringIO(line), skipinitialspace=True))
        except (csv.Error, StopIteration) as exc:
            raise VdtParseError(f"Invalid VDT record: {raw}") from exc
        if len(values) != 4:
            continue
        region, process, commodity, direction = (value.strip() for value in values)
        direction = direction.upper()
        if direction not in _DIRECTIONS:
            continue
        if not process or not commodity:
            continue
        rows.append((region, process, commodity, direction))

    if not rows:
        raise VdtParseError("No valid IN/OUT commodity-flow records found in VDT data.")
    return pd.DataFrame(rows, columns=VDT_COLUMNS).drop_duplicates().reset_index(drop=True)


def load_vdt(path: Path | str) -> pd.DataFrame:
    """Load one VDT file as a validated, de-duplicated flow topology table."""
    path = Path(path)
    if not path.is_file():
        raise VdtParseError(f"VDT file is unavailable: {path}")
    try:
        return parse_vdt_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        raise VdtParseError(f"Could not read VDT file: {path}") from exc


def process_flow_topology(vdt: pd.DataFrame) -> pd.DataFrame:
    """Summarise each process's structural input and output commodities."""
    required = set(VDT_COLUMNS)
    if not required.issubset(vdt.columns):
        raise ValueError("VDT topology must contain region, process, commodity, and direction.")
    if vdt.empty:
        return pd.DataFrame(columns=["process", "input_commodities", "output_commodities"])

    inputs = (
        vdt[vdt["direction"].eq("IN")].groupby("process")["commodity"]
        .agg(lambda values: ", ".join(sorted(set(values)))).rename("input_commodities")
    )
    outputs = (
        vdt[vdt["direction"].eq("OUT")].groupby("process")["commodity"]
        .agg(lambda values: ", ".join(sorted(set(values)))).rename("output_commodities")
    )
    return pd.concat([inputs, outputs], axis=1).fillna("").reset_index()
