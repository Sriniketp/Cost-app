"""Curated resource-pool source identity.

This small dependency-free module lets the Streamlit shell invalidate derived
allocation and workbook caches without importing the full allocation engine.
"""
from __future__ import annotations

from pathlib import Path


DEFAULT_RESOURCE_POOLS_PATH = (
    Path(__file__).parent.parent / "lookup_tables" / "resource_pools.csv"
)

# Demand-service pools in the curated sheet group end-use demands (DEM set)
# for reference only.  They are not fuels, so they never enter a fuel-cost
# split; those commodities are treated like any other unpooled demand service.
DEMAND_SERVICE_POOLS = frozenset({
    "Agriculture demand",
    "Industry demand",
    "Residential demand",
    "Services demand",
})

# Each pool declares one ``pool_kind`` in the curated sheet.  Fuel pools split
# a fuel's supply cost by sectors that burned it; service pools split a
# delivered service by sectors that consumed it.  The two measure different
# things and are never combined.  Demand pools are reference groupings only.
POOL_KIND_FUEL = "Fuel"
POOL_KIND_SERVICE = "Service"
POOL_KIND_DEMAND = "Demand"
SPLIT_POOL_KINDS = (POOL_KIND_FUEL, POOL_KIND_SERVICE)
POOL_KINDS = (*SPLIT_POOL_KINDS, POOL_KIND_DEMAND)


def resource_pools_fingerprint(
    path: Path | str = DEFAULT_RESOURCE_POOLS_PATH,
) -> tuple[str, int, int]:
    """Return source metadata that invalidates results when the pool sheet changes.

    A missing pools file raises rather than producing a valid-looking cache
    key for absent data.
    """
    source = Path(path)
    stat = source.stat()
    return (str(source), stat.st_size, stat.st_mtime_ns)
