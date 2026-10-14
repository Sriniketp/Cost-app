"""
Shared colour palettes for sector, supply and component categories.

Both the Streamlit dashboard (app.py) and the CLI output generator
(outputs.py) import from here so that charts remain consistent.

Sector colours match the vistool palette used in the broader UK TIMES
reporting ecosystem.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence

import pandas as pd


# The official DESNZ categorical palette, in its stated order of preference.
# The first nine colours are primary hues; the remaining nine are their lighter
# overflow counterparts. Keep this sequence aligned with
# ``DESNZ_colour_palette.R`` in the UK TIMES visualisation tooling.
DESNZ_COLOURS: tuple[str, ...] = (
    "#003479", "#62C3F4", "#FFB92D", "#F58220", "#00A9A5", "#DD2745",
    "#756AA1", "#5A2035", "#2F9E44", "#4D7AB8", "#A8E1FA", "#FFD77A",
    "#F9A860", "#66D4D1", "#E86580", "#A799C7", "#8F5067", "#6FCF7E",
)
MAX_VISIBLE_CATEGORIES = len(DESNZ_COLOURS)
TOP_VISIBLE_CATEGORIES = MAX_VISIBLE_CATEGORIES - 1
OTHER_LABEL = "Other"

# The styles distinguish non-additive identities (for example runs and
# investment vintages) after the 18 available categorical hues are exhausted.
# They intentionally contain no Plotly imports so selector-stage code stays
# lightweight.
_OVERFLOW_DASHES = ("dash", "dot", "dashdot", "longdash", "longdashdot")
_OVERFLOW_PATTERNS = ("/", "\\", "x", "-", "|", "+", ".")

# Carbon credits and dummy imports are drawn as the top segment of stacked
# column charts so they are easy to spot. These are every label spelling they
# carry across the process taxonomy and the cost-nature buckets; matching is
# case-insensitive.
PINNED_TOP_CATEGORIES: frozenset[str] = frozenset({
    "carbon credits", "emission credits", "co2 credits",
    "dummy imports", "_dummy imports",
})


def is_pinned_category(label: object) -> bool:
    """Return True when *label* is a carbon-credit or dummy-import category."""
    return str(label).strip().casefold() in PINNED_TOP_CATEGORIES


def pin_categories_last(categories: Iterable[object]) -> list:
    """Return *categories* with pinned labels moved to the end, otherwise order-preserving."""
    items = list(categories)
    return [c for c in items if not is_pinned_category(c)] + [c for c in items if is_pinned_category(c)]


def desnz_colour(index: int) -> str:
    """Return one DESNZ colour for a non-negative zero-based index."""
    if index < 0:
        raise ValueError("index must be non-negative.")
    return DESNZ_COLOURS[index % MAX_VISIBLE_CATEGORIES]


def desnz_colour_map(
    categories: Iterable[object],
    *,
    preferred_order: Sequence[object] | None = None,
) -> dict[str, str]:
    """Assign stable, non-repeating DESNZ hues through 18 visible categories.

    A supplied semantic ``preferred_order`` takes precedence; remaining live
    labels are ordered case-insensitively so data-frame order cannot alter a
    category's colour on a rerun.
    """
    labels = {
        str(category).strip()
        for category in categories
        if str(category).strip()
    }
    ordered: list[str] = []
    if preferred_order is not None:
        for category in preferred_order:
            label = str(category).strip()
            if label in labels and label not in ordered:
                ordered.append(label)
    ordered.extend(sorted(labels.difference(ordered), key=str.casefold))
    return {
        category: desnz_colour(index)
        for index, category in enumerate(ordered)
    }


def desnz_overflow_style(index: int) -> dict[str, str]:
    """Return deterministic Plotly-compatible styling after palette overflow."""
    if index < MAX_VISIBLE_CATEGORIES:
        return {"dash": "solid", "pattern": ""}
    overflow_index = index - MAX_VISIBLE_CATEGORIES
    return {
        "dash": _OVERFLOW_DASHES[overflow_index % len(_OVERFLOW_DASHES)],
        "pattern": _OVERFLOW_PATTERNS[overflow_index % len(_OVERFLOW_PATTERNS)],
    }


def collapse_minor_categories(
    frame: pd.DataFrame,
    *,
    category: str,
    value: str,
    group_by: Iterable[str],
    keep: int = TOP_VISIBLE_CATEGORIES,
    other_label: str = OTHER_LABEL,
    always_keep: Iterable[str] = (),
) -> pd.DataFrame:
    """Keep major additive categories and combine the remainder into ``Other``.

    Ranking is by absolute contribution so material credits and scenario deltas
    are retained. Labels in ``always_keep`` (case-insensitive) are never folded
    into ``Other``; they use up slots of the ``keep`` budget. The returned rows
    contain only ``group_by``, ``category`` and the signed ``value``; their
    total always equals the input total.
    """
    if not 1 <= keep < MAX_VISIBLE_CATEGORIES:
        raise ValueError(
            f"keep must be between 1 and {MAX_VISIBLE_CATEGORIES - 1}."
        )
    group_columns = list(dict.fromkeys(group_by))
    required = set(group_columns) | {category, value}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError("Missing category-collapse columns: " + ", ".join(sorted(missing)))
    if frame.empty:
        return frame.loc[:, [*group_columns, category, value]].copy()

    result = frame.loc[:, [*group_columns, category, value]].copy()
    result[category] = result[category].fillna("").astype(str).str.strip().replace("", "Unlabelled")
    result[value] = pd.to_numeric(result[value], errors="coerce").fillna(0.0)
    totals = result.groupby(category, dropna=False)[value].sum()
    ranking = pd.DataFrame({
        category: totals.index.astype(str),
        "_absolute_total": totals.abs().to_numpy(),
    }).sort_values(["_absolute_total", category], ascending=[False, True], key=lambda series: (
        series.map(str.casefold) if series.name == category else series
    ))
    protected_labels = {str(label).strip().casefold() for label in always_keep}
    is_protected = ranking[category].map(lambda label: label.casefold() in protected_labels)
    protected = list(ranking.loc[is_protected, category])[:keep]
    others = ranking.loc[~is_protected, category].head(keep - len(protected))
    retained = set(protected) | set(others)
    result.loc[~result[category].isin(retained), category] = other_label
    return result.groupby([*group_columns, category], as_index=False, dropna=False)[value].sum()

# ---------------------------------------------------------------------------
# End-use and supply sector colours
# app.py uses this for the interactive dashboard; outputs.py for static charts.
# Keys cover all sectors that can appear in TIMES cost attribution.
# ---------------------------------------------------------------------------

SECTOR_COLOURS: dict[str, str] = {
    # Core end-use sectors
    "Residential":           "#003479",
    "Transport":             "#F58220",
    "Industry":              "#756AA1",
    "Services":              "#00A9A5",
    "Agriculture":           "#2F9E44",
    # Supply / infrastructure
    "Electricity":           "#FFB92D",
    "Processing":            "#62C3F4",
    "Resources":             "#5A2035",
    "Other":                 "#A799C7",
    # Multi / summary
    "Multiple":              "#4D7AB8",
    # Special vistool categories
    "CO2 Credits":           "#66D4D1",
    "Engineered Removals":   "#6FCF7E",
    "Waste":                 "#8F5067",
    "_DUMMY IMPORTS":        "#A8E1FA",
    "_Air quality damage costs": "#E86580",
    "_Missing sector!":      "#DD2745",
}

# ---------------------------------------------------------------------------
# Supply sub-category colours (used in supply-side breakdown charts)
# ---------------------------------------------------------------------------

SUPPLY_COLOURS: dict[str, str] = {
    "Fuel imports":          "#5A2035",
    "Oil refining":          "#F58220",
    "Power generation":      "#FFB92D",
    "Biomass supply":        "#2F9E44",
    "Hydrogen production":   "#00A9A5",
    "Grid & networks":       "#4D7AB8",
    "CCS infrastructure":    "#756AA1",
    "Other supply":          "#A799C7",
}

# ---------------------------------------------------------------------------
# Cost component colours (resource vs policy breakdown)
# ---------------------------------------------------------------------------

COMPONENT_COLOURS: dict[str, str] = {
    "Direct Resource":           "#003479",
    "Upstream Resource":         "#62C3F4",
    "Infrastructure Resource":   "#4D7AB8",
    "Policy Transfers":          "#FFB92D",
    "Other":                     "#A799C7",
}
COST_COMPONENT_COLOURS: dict[str, str] = {
    "Cost_Inv": "#003479",
    "Cost_Fom": "#62C3F4",
    "Cost_Act": "#F58220",
    "Cost_Flo": "#FFB92D",
}


PATHWAY_MIXED_COLOUR = "#756AA1"
PATHWAY_UNLABELLED_COLOUR = "#A8E1FA"
SEMANTIC_COLOURS: dict[str, str] = {
    "cost": "#003479",
    "credit": "#5A2035",
    "matched": "#2F9E44",
    "warning": "#FFB92D",
    "error": "#DD2745",
    "origin_supply": "#003479",
    "import": "#62C3F4",
    "distribution": "#FFB92D",
    "transformation": "#756AA1",
    "storage": "#00A9A5",
    "final_consumption": "#2F9E44",
    "export": "#DD2745",
    "unclassified": "#A799C7",
}


def pathway_taxonomy_colour(category: object, *, fallback: str) -> str:
    """Return a stable colour for a live pathway taxonomy category."""
    label = str(category or "").strip()
    if not label:
        return fallback
    if label == "Mixed":
        return PATHWAY_MIXED_COLOUR
    if label == "Unlabelled":
        return PATHWAY_UNLABELLED_COLOUR
    if label in SECTOR_COLOURS:
        return SECTOR_COLOURS[label]
    digest = hashlib.sha256(label.casefold().encode("utf-8")).hexdigest()
    return desnz_colour(int(digest, 16))
