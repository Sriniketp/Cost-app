"""
Shared UI helpers for the Streamlit dashboard.

Provides chart rendering, display-name mappings, run colours,
and the RunBundle wrapper used by all tab modules.
"""

from __future__ import annotations

import math

import streamlit as st

from cost_allocation.colours import DESNZ_COLOURS, MAX_VISIBLE_CATEGORIES, desnz_overflow_style
from cost_allocation.run_bundle import APP_BUILD, RunBundle, _run_colour
from cost_allocation.price_year import bundle_money_unit, money_unit

# Single source of the optional Plotly binding for every Streamlit tab module.
# Tabs import ``HAS_PLOTLY`` and ``go``/``px`` from here rather than repeating
# the guarded import. When Plotly is unavailable the names are bound to ``None``
# so importers stay valid; every render path is gated on ``HAS_PLOTLY`` first.
#
# ``outputs.py`` deliberately keeps its own copy of this guard: it is imported
# by the CLI entry point and must not depend on this Streamlit-importing module.
try:
    import plotly.graph_objects as go
    import plotly.express as px
    from plotly.subplots import make_subplots  # noqa: F401
    HAS_PLOTLY = True
except ImportError:
    go = None
    px = None
    make_subplots = None
    HAS_PLOTLY = False


# ---------------------------------------------------------------------------
# Chart rendering
# ---------------------------------------------------------------------------

def _is_point_sequence(value) -> bool:
    """Return whether a Plotly coordinate value represents multiple points."""
    return (
        not isinstance(value, (str, bytes))
        and hasattr(value, "__iter__")
        and hasattr(value, "__len__")
    )


def _is_axis_domain_anchor(trace) -> bool:
    """Return whether a trace exists solely to establish category ordering."""
    metadata = getattr(trace, "meta", None)
    return isinstance(metadata, dict) and metadata.get("axis_domain_anchor") is True


def _ordered_period_labels(periods) -> list[str]:
    """Return distinct period labels in numeric order for a category axis."""
    labels: dict[str, float] = {}
    for period in periods:
        if period is None:
            continue
        try:
            numeric = float(period)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(numeric):
            continue
        label = str(int(numeric)) if numeric.is_integer() else str(numeric)
        labels[label] = numeric
    return [label for label, _ in sorted(labels.items(), key=lambda item: item[1])]


def _apply_period_category_order(fig, periods) -> list[str]:
    """Lock a categorical time axis to the supplied chronological periods.

    Plotly otherwise uses the order in which categories first appear across
    traces. A filtered chart may therefore append an early year after a sparse
    late-year trace. This helper is deliberately opt-in so ordinary text
    category axes, such as taxonomy charts, retain their native ordering.
    """
    labels = _ordered_period_labels(periods)
    if labels:
        fig.update_xaxes(
            type="category",
            categoryorder="array",
            categoryarray=labels,
        )
    return labels


def _apply_period_run_category_order(fig, periods, run_ids) -> None:
    """Lock a sparse ``Period · Run`` axis to chronological period/run order.

    Plotly does not expose an independent ordering array for each level of a
    multicategory axis. An invisible zero-value bar therefore establishes the
    complete Cartesian category domain *before* chart data are encountered.
    Existing rendering helpers recognise the metadata flag and exclude this
    synthetic trace from legends, hover totals and visual styling.
    """
    labels = _ordered_period_labels(periods)
    runs = list(dict.fromkeys(
        str(run_id).strip() for run_id in run_ids if str(run_id).strip()
    ))
    if not labels or not runs:
        return

    existing = tuple(trace for trace in fig.data if not _is_axis_domain_anchor(trace))
    fig.data = existing
    fig.add_trace(go.Bar(
        x=[
            [label for label in labels for _ in runs],
            [run_id for _ in labels for run_id in runs],
        ],
        y=[0.0] * (len(labels) * len(runs)),
        name="",
        showlegend=False,
        hoverinfo="skip",
        marker={"opacity": 0},
        meta={"axis_domain_anchor": True},
    ))
    fig.data = (fig.data[-1], *existing)
    fig.update_xaxes(type="multicategory")


def _supports_trace_property(trace, property_name: str) -> bool:
    """Return whether a Plotly trace accepts a given top-level property."""
    valid_properties = getattr(trace, "_valid_props", ())
    return property_name in valid_properties


def _has_legend_label(trace) -> bool:
    """Return whether a trace has a meaningful label safe to show in a legend."""
    return (
        _supports_trace_property(trace, "showlegend")
        and isinstance(getattr(trace, "name", None), str)
        and bool(trace.name.strip())
    )


def _trace_point_keys(trace, point_count: int) -> list:
    """Return hashable x-axis keys, including Plotly multicategory axes."""
    x_values = trace.x
    if x_values is None:
        return []
    if (
        isinstance(x_values, (list, tuple))
        and x_values
        and all(_is_point_sequence(level) and len(level) == point_count for level in x_values)
    ):
        return list(zip(*(list(level) for level in x_values)))
    return list(x_values)


def _append_point_customdata(trace, values: list[float]) -> int:
    """Append one per-point customdata field and return its column index."""
    existing = trace.customdata
    if existing is None:
        trace.customdata = [[value] for value in values]
        return 0

    rows = list(existing)
    if not rows:
        trace.customdata = [[value] for value in values]
        return 0
    first = rows[0]
    if _is_point_sequence(first):
        column_index = len(first)
        trace.customdata = [list(row) + [value] for row, value in zip(rows, values)]
        return column_index

    trace.customdata = [[row, value] for row, value in zip(rows, values)]
    return 1


def _append_hover_line(template: str, line: str) -> str:
    """Insert a hover line before Plotly's optional extra-box section."""
    if line in template:
        return template
    if "<extra" in template:
        return template.replace("<extra", f"<br>{line}<extra", 1)
    return f"{template}<br>{line}"


def _add_stack_totals_to_hover(fig) -> None:
    """Attach each stacked point's complete x-position total to its hover data."""
    bar_traces = [
        trace for trace in fig.data
        if trace.type == "bar" and not _is_axis_domain_anchor(trace)
    ]
    stacked_bars = fig.layout.barmode in {"stack", "relative"} and bool(bar_traces)
    stacked_areas = [
        trace for trace in fig.data
        if trace.type == "scatter" and getattr(trace, "stackgroup", None)
    ]

    trace_groups: list[list] = []
    if stacked_bars:
        trace_groups.append(bar_traces)
    for stackgroup in {trace.stackgroup for trace in stacked_areas}:
        trace_groups.append([
            trace for trace in stacked_areas if trace.stackgroup == stackgroup
        ])

    for traces in trace_groups:
        totals: dict[tuple, float] = {}
        trace_points: list[tuple[object, list, list]] = []
        for trace in traces:
            y_values = list(trace.y) if trace.y is not None else []
            point_keys = _trace_point_keys(trace, len(y_values))
            if len(point_keys) != len(y_values):
                continue
            axis_key = (
                getattr(trace, "xaxis", None) or "x",
                getattr(trace, "yaxis", None) or "y",
                getattr(trace, "offsetgroup", None) or "",
            )
            trace_points.append((trace, point_keys, y_values))
            for point_key, value in zip(point_keys, y_values):
                try:
                    numeric_value = float(value)
                except (TypeError, ValueError):
                    continue
                total_key = (*axis_key, point_key)
                totals[total_key] = totals.get(total_key, 0.0) + numeric_value

        for trace, point_keys, _ in trace_points:
            axis_key = (
                getattr(trace, "xaxis", None) or "x",
                getattr(trace, "yaxis", None) or "y",
                getattr(trace, "offsetgroup", None) or "",
            )
            point_totals = [totals.get((*axis_key, point_key), 0.0) for point_key in point_keys]
            customdata_index = _append_point_customdata(trace, point_totals)
            trace.hovertemplate = _append_hover_line(
                trace.hovertemplate or "%{fullData.name}<br>Value: %{y:,.2f}<extra></extra>",
                f"Total: %{{customdata[{customdata_index}]:,.3f}}",
            )


def _chart_x_axis_profile(fig) -> tuple[bool, int, int]:
    """Return Cartesian status, the x tick angle, and the bottom margin needed.

    On a Plotly multi-category axis the inner level holds the run name and the
    outer level stays horizontal, so rotating the inner labels shows every run
    name in full however many run and period columns are drawn.
    """
    has_cartesian_data = False
    has_multicategory = False
    longest_label = 0
    longest_run_label = 0
    for trace in fig.data:
        if trace.type in {"bar", "scatter", "waterfall", "box", "violin", "histogram"}:
            has_cartesian_data = True
        x_values = getattr(trace, "x", None)
        if x_values is None:
            continue
        if (
            isinstance(x_values, (list, tuple))
            and x_values
            and all(_is_point_sequence(level) for level in x_values)
        ):
            has_multicategory = True
            inner_labels = list(x_values[-1])
            if inner_labels:
                longest_run_label = max(
                    longest_run_label,
                    max(len(str(value)) for value in inner_labels),
                )
            continue
        try:
            values = list(x_values)
        except TypeError:
            continue
        if values:
            longest_label = max(longest_label, max(len(str(value)) for value in values))
    if has_multicategory and longest_run_label > 8:
        return has_cartesian_data, -90, min(340, 104 + longest_run_label * 8)
    if longest_label > 14:
        return has_cartesian_data, -35, 132
    return has_cartesian_data, 0, 92


def _apply_chart_readability_defaults(fig, *, legend_position: str = "auto") -> None:
    """Apply a consistent accessible presentation to all dashboard figures."""
    if legend_position not in {"auto", "right", "right_wide"}:
        raise ValueError("legend_position must be 'auto', 'right', or 'right_wide'.")
    for trace in fig.data:
        if _supports_trace_property(trace, "showlegend") and not _has_legend_label(trace):
            trace.showlegend = False
    has_cartesian_data, tick_angle, bottom_margin = _chart_x_axis_profile(fig)
    trace_count = len({
        trace.name for trace in fig.data
        if _has_legend_label(trace) and trace.showlegend is not False
    })
    existing_margin = fig.layout.margin
    margin = {
        "l": max(78, int(existing_margin.l or 0)),
        "r": max(32, int(existing_margin.r or 0)),
        "t": max(82, int(existing_margin.t or 0)),
        "b": max(bottom_margin, int(existing_margin.b or 0)),
    }
    layout = {
        "template": "plotly_white",
        "colorway": list(DESNZ_COLOURS),
        "font": {"family": "Arial, sans-serif", "size": 14, "color": "#172B4D"},
        "margin": margin,
        "hoverlabel": {
            "bgcolor": "white",
            "bordercolor": "#94A3B8",
            "font_size": 13,
            "font_family": "Arial, sans-serif",
            "font_color": "#172B4D",
            "namelength": -1,
        },
    }
    title_text = getattr(fig.layout.title, "text", None)
    if isinstance(title_text, str) and title_text.strip():
        layout["title"] = {
            "text": title_text,
            "font": {"size": 20, "color": "#172B4D"},
            "x": 0.01,
            "xanchor": "left",
        }
    has_multicategory = bool(
        getattr(fig.layout.xaxis, "type", None) == "multicategory"
        or any(
            isinstance(getattr(trace, "x", None), (list, tuple))
            and getattr(trace, "x", None)
            and all(_is_point_sequence(level) for level in getattr(trace, "x", None))
            for trace in fig.data
        )
    )
    if has_cartesian_data:
        current_height = int(fig.layout.height or 0)
        base_height = max(500, current_height)
        base_bottom = max(92, int(existing_margin.b or 0))
        # Keep the plotting panel at least as tall as the default layout when
        # long labels require extra margin by growing the total chart height.
        layout["height"] = base_height + max(0, bottom_margin - base_bottom)
    if trace_count and (legend_position in {"right", "right_wide"} or trace_count > 4):
        legend_x = 1.14 if legend_position == "right_wide" else 1.02
        right_margin = 350 if legend_position == "right_wide" else 210
        layout["legend"] = {
            "orientation": "v", "yanchor": "top", "y": 1,
            "xanchor": "left", "x": legend_x, "bgcolor": "rgba(255,255,255,0.92)",
            "bordercolor": "#CBD5E1", "borderwidth": 1, "font": {"size": 12},
            "itemsizing": "constant",
        }
        layout["margin"]["r"] = max(layout["margin"]["r"], right_margin)
        layout["margin"]["t"] = max(layout["margin"]["t"], 96)
    elif trace_count:
        if has_multicategory:
            layout["legend"] = {
                "orientation": "v", "yanchor": "top", "y": 1,
                "xanchor": "left", "x": 1.02, "bgcolor": "rgba(255,255,255,0.92)",
                "bordercolor": "#CBD5E1", "borderwidth": 1, "font": {"size": 12},
                "itemsizing": "constant",
            }
            layout["margin"]["r"] = max(layout["margin"]["r"], 210)
            layout["margin"]["t"] = max(layout["margin"]["t"], 96)
        else:
            layout["legend"] = {
                "orientation": "h", "yanchor": "bottom", "y": 1.02,
                "xanchor": "left", "x": 0, "font": {"size": 12},
                "itemsizing": "constant",
            }
    fig.update_layout(**layout)
    if has_cartesian_data:
        fig.update_xaxes(
            showgrid=False,
            automargin=True,
            tickangle=tick_angle,
            tickfont={"size": 12, "color": "#334155"},
            title_font={"size": 14, "color": "#172B4D"},
            title_standoff=14,
        )
        for axis in fig.select_yaxes():
            axis.update(
                automargin=True,
                tickfont={"size": 12, "color": "#334155"},
                title_font={"size": 14, "color": "#172B4D"},
                title_standoff=14,
            )
            if axis.overlaying:
                # A secondary scale has independent tick positions. Drawing its
                # guides over the primary scale produces a second, uneven set of
                # horizontal lines, so preserve an explicit choice or default
                # the secondary guides and baseline to hidden.
                if axis.showgrid is None:
                    axis.showgrid = False
                if axis.zeroline is None:
                    axis.zeroline = False
                continue
            axis.update(showgrid=True, gridcolor="#E2E8F0", zerolinecolor="#94A3B8")
            if axis.zeroline is None:
                axis.zeroline = True
        for trace in fig.data:
            if (
                trace.type == "bar"
                and not _is_axis_domain_anchor(trace)
                and trace.marker.line.width is None
            ):
                trace.marker.line = {"color": "rgba(255,255,255,0.9)", "width": 0.6}
    uncoloured_trace_index = 0
    for trace in fig.data:
        if _is_axis_domain_anchor(trace) or not _has_legend_label(trace):
            continue
        marker_colour = getattr(getattr(trace, "marker", None), "color", None)
        line_colour = getattr(getattr(trace, "line", None), "color", None)
        fill_colour = getattr(trace, "fillcolor", None)
        has_explicit_colour = any(
            colour is not None and str(colour).strip()
            for colour in (marker_colour, line_colour, fill_colour)
        )
        if not has_explicit_colour and uncoloured_trace_index >= MAX_VISIBLE_CATEGORIES:
            style = desnz_overflow_style(uncoloured_trace_index)
            if trace.type == "bar":
                trace.marker.pattern.shape = style["pattern"]
            elif trace.type == "scatter":
                trace.line.dash = style["dash"]
        uncoloured_trace_index += 1


def _plotly_chart(fig, *, legend_position: str = "auto") -> None:
    """Render a Plotly figure at its stable Streamlit element position."""
    if not HAS_PLOTLY:
        st.info("Plotly is not available. Install with: pip install plotly")
        return

    _apply_chart_readability_defaults(fig, legend_position=legend_position)
    for trace in fig.data:
        if _is_axis_domain_anchor(trace):
            continue
        if not hasattr(trace, "hovertemplate"):
            continue
        if trace.hovertemplate is None:
            if hasattr(trace, "stackgroup") and trace.stackgroup:
                trace.hovertemplate = (
                    "%{fullData.name}<br>"
                    "Period: %{x}<br>"
                    "Value: %{y:,.2f}<extra></extra>"
                )
            elif trace.type == "bar":
                trace.hovertemplate = (
                    "%{fullData.name}<br>"
                    "Period: %{x}<br>"
                    "Cost: %{y:,.1f} £M (price year shown on the chart axis)<extra></extra>"
                )
            else:
                trace.hovertemplate = (
                    "%{fullData.name}<br>"
                    "Period: %{x}<br>"
                    "Value: %{y:,.2f}<extra></extra>"
                )
    _add_stack_totals_to_hover(fig)
    st.plotly_chart(
        fig,
        width="stretch",
        config={
            "displaylogo": False,
            "responsive": True,
            "scrollZoom": False,
        },
    )


# ---------------------------------------------------------------------------
# Display-friendly attribute names
# ---------------------------------------------------------------------------

ATTR_DISPLAY: dict[str, str] = {
    "Cost_Inv": "Investment (Cost_Inv)",
    "Cost_Fom": "Fixed O&M (Cost_Fom)",
    "Cost_Act": "Variable O&M (Cost_Act)",
    "Cost_Flo": "Fuel & Flow (Cost_Flo)",
    "Cost_Com": "Commodity (Cost_Com)",
    "Cost_Salv": "Salvage (Cost_Salv)",
    "ObjZ": "System Objective (ObjZ)",
    "Reg_ACost": "Regional Annual Cost (Reg_ACost)",
    "Time_NPV": "NPV Factor (Time_NPV)",
    "VAR_Ncap": "New Capacity (VAR_Ncap)",
    "VAR_Cap": "Installed Capacity (VAR_Cap)",
    "VAR_Act": "Activity (VAR_Act)",
    "Cap_New": "Capacity Addition (Cap_New)",
    "NCAP_COST": "Capital Cost (NCAP_COST)",
    "NCAP_FOM": "Fixed O&M Cost (NCAP_FOM)",
    "ACT_COST": "Activity Cost (ACT_COST)",
    "NCAP_DRATE": "Discount Rate (NCAP_DRATE)",
    "NCAP_ELIFE": "Economic Life (NCAP_ELIFE)",
    "NCAP_TLIFE": "Technical Life (NCAP_TLIFE)",
    "NCAP_ILED": "Lead Time (NCAP_ILED)",
}


def _dn(attr: str) -> str:
    """Return natural-English display name for a TIMES attribute code."""
    return ATTR_DISPLAY.get(attr, attr)



