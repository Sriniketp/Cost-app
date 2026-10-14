"""Full-chain cascade of pool costs through energy conversion processes.

A process that uses one pool and produces another (a gas power station, an
electrolyser, a hydrogen turbine, a boiler making a heat service) is a
*converter*.  Its share of the input pool's cost is not booked to its own
sector; it is passed on to the pools it produces, split by observed output TWh
— the same output shares that already assign the process's own costs.  Only
true final use books cost to a sector.

Because conversions form loops (electricity makes hydrogen, hydrogen makes
electricity), every pool of one model year is solved together.  With ``T[k, r]``
the share of pool ``k``'s use taken by converters producing ``r``, the cost
entering each pool for an injection ``d`` of own source cost is

    x = d + Tᵀx,  so  x = (I - Tᵀ)⁻¹ d.

This module is pure: it does not import Streamlit and does not read files.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


CASCADE_ROLE_FINAL = "Final use"
CASCADE_ROLE_PASSED_ON = "Conversion"
CASCADE_ROLE_UNALLOCATED = "Unallocated"
CASCADE_STATUS_SOLVED = "Solved"
CASCADE_STATUS_FAILED = "Solve failed; converters treated as final use"

PROCESS_ALLOCATION_COLUMNS = (
    "period", "view", "sign", "resource_pool", "region", "process", "sector",
    "role", "passed_to_pool", "allocated_cost_m",
)
CASCADE_ALLOCATION_COLUMNS = (
    "period", "view", "origin_pool", "resource_pool", "sector", "allocated_cost_m",
)
CASCADE_UNALLOCATED_COLUMNS = (
    "period", "view", "resource_pool", "cascade_unallocated_cost_m",
)
ORIGIN_SECTOR_SHARE_COLUMNS = (
    "share_basis_year", "resource_pool", "sector", "cascade_share",
)
_CONSUMER_COLUMNS = (
    "resource_pool", "region", "process", "sector", "role", "passed_to_pool", "share",
)


@dataclass(frozen=True)
class CascadeNetwork:
    """The solved conversion network of one model year.

    ``multipliers[r, k]`` is the cost entering pool ``r`` per unit of own cost
    injected into pool ``k``; ``final_shares[r, s]`` is the share of pool
    ``r``'s use that is final use by sector ``s``.  ``consumer_shares`` holds
    one row per consumer (and, for a converter, per output pool), and its
    shares sum to one for every solvable pool.  Pools outside ``pools`` either
    have no positive use this year or cannot reach final use; cost entering
    them stays unallocated.
    """

    year: int
    pools: tuple[str, ...]
    sectors: tuple[str, ...]
    multipliers: np.ndarray
    final_shares: np.ndarray
    consumer_shares: pd.DataFrame
    status: str


@dataclass(frozen=True)
class CascadeOutcome:
    """Cascaded cost for every injected group, summed over share-basis years."""

    process_allocations: pd.DataFrame
    cascade_allocations: pd.DataFrame
    cascade_unallocated: pd.DataFrame


def _diagnostic(
    diagnostic_type: str, period: int, process: str, commodity: str, detail: str,
    value_m: object = pd.NA,
) -> dict:
    return {
        "diagnostic_type": diagnostic_type, "period": period, "process": process,
        "commodity": commodity, "detail": detail, "value_m": value_m,
    }


def _draining_pools(
    pools: list[str],
    transfers: pd.Series,
    final_fraction: pd.Series,
    tolerance: float,
) -> set[str]:
    """Return pools from which some cost can reach final use."""
    draining = {pool for pool in pools if abs(float(final_fraction.get(pool, 0.0))) > tolerance}
    feeders: dict[str, set[str]] = {}
    for (source, target), share in transfers.items():
        if abs(float(share)) > tolerance:
            feeders.setdefault(target, set()).add(source)
    frontier = list(draining)
    while frontier:
        target = frontier.pop()
        for source in feeders.get(target, ()):
            if source not in draining:
                draining.add(source)
                frontier.append(source)
    return draining


def _solve_multipliers(transfer: np.ndarray) -> np.ndarray | None:
    """Return ``(I - Tᵀ)⁻¹`` with one refinement step, or ``None`` if singular."""
    size = transfer.shape[0]
    identity = np.eye(size)
    system = identity - transfer.T
    try:
        inverse = np.linalg.solve(system, identity)
    except np.linalg.LinAlgError:
        return None
    # One step of iterative refinement keeps per-pool balances exact to
    # floating-point precision even through long conversion loops.
    inverse = inverse + inverse @ (identity - system @ inverse)
    if not np.all(np.isfinite(inverse)):
        return None
    return inverse


def build_cascade_network(
    year: int,
    consumers: pd.DataFrame,
    output_lookup: dict[tuple[int, str, str], tuple[dict[str, float], bool]],
    *,
    demand_output_keys: set[tuple[int, str, str]] | frozenset = frozenset(),
    tolerance: float = 1e-9,
) -> tuple[CascadeNetwork, list[dict]]:
    """Build and solve one model year's conversion network.

    ``consumers`` holds included signed use per consumer: ``resource_pool``,
    ``region``, ``process``, ``sector`` and ``quantity_twh``.  The allocation
    denominator of each pool is unchanged from the one-step method; only the
    destination of a converter's share differs.
    """
    diagnostics: list[dict] = []
    year = int(year)
    rows = consumers.loc[:, ["resource_pool", "region", "process", "sector", "quantity_twh"]].copy()
    rows["quantity_twh"] = pd.to_numeric(rows["quantity_twh"], errors="coerce").fillna(0.0)
    rows = rows.groupby(
        ["resource_pool", "region", "process", "sector"], as_index=False, dropna=False
    )["quantity_twh"].sum()
    totals = rows.groupby("resource_pool")["quantity_twh"].sum()
    positive = totals[totals.gt(tolerance)]
    rows = rows[rows["resource_pool"].isin(positive.index)].copy()
    rows["share"] = rows["quantity_twh"] / rows["resource_pool"].map(positive)

    output_records: list[tuple[str, str, str, float]] = []
    for region, process in rows[["region", "process"]].drop_duplicates().itertuples(index=False):
        key = (year, str(region), str(process))
        weights, ambiguous = output_lookup.get(key, ({}, False))
        if ambiguous:
            pools = ", ".join(sorted(rows.loc[
                rows["region"].eq(region) & rows["process"].eq(process), "resource_pool"
            ].unique()))
            diagnostics.append(_diagnostic(
                "cascade_converter_output_unclear", year, str(process), pools,
                "Input cost stays with this process's sector because its output includes "
                "unmapped or incompatible energy, so the output split cannot be observed.",
            ))
            continue
        total = sum(weights.values())
        if not weights or total <= tolerance:
            continue
        if key in demand_output_keys:
            diagnostics.append(_diagnostic(
                "cascade_converter_has_demand_output", year, str(process),
                ", ".join(sorted(weights)),
                "This process also delivers a demand service; its input cost follows "
                "its energy outputs only, as its own costs do.",
            ))
        for pool, weight in weights.items():
            output_records.append((region, process, pool, weight / total))
    outputs = pd.DataFrame(
        output_records, columns=["region", "process", "passed_to_pool", "output_share"]
    ).astype({"output_share": float})
    consumer_rows = rows.merge(outputs, on=["region", "process"], how="left")
    passed = consumer_rows["passed_to_pool"].notna()
    consumer_rows["role"] = np.where(passed, CASCADE_ROLE_PASSED_ON, CASCADE_ROLE_FINAL)
    consumer_rows["passed_to_pool"] = consumer_rows["passed_to_pool"].fillna("")
    consumer_rows["share"] = consumer_rows["share"] * consumer_rows["output_share"].fillna(1.0)

    candidates = sorted(positive.index, key=str)
    transfers = consumer_rows[passed & consumer_rows["passed_to_pool"].isin(candidates)].groupby(
        ["resource_pool", "passed_to_pool"]
    )["share"].sum()
    final_fraction = consumer_rows[~passed].groupby("resource_pool")["share"].sum()
    draining = _draining_pools(candidates, transfers, final_fraction, tolerance)
    for pool in candidates:
        if pool not in draining:
            diagnostics.append(_diagnostic(
                "cascade_pool_without_final_use", year, "", pool,
                "Every use of this pool is conversion into pools that never reach final "
                "use, so cost entering it stays unallocated.",
            ))
    pools = [pool for pool in candidates if pool in draining]
    index = {pool: position for position, pool in enumerate(pools)}

    status = CASCADE_STATUS_SOLVED
    transfer = np.zeros((len(pools), len(pools)))
    for (source, target), share in transfers.items():
        if source in index and target in index:
            transfer[index[source], index[target]] += float(share)
    multipliers = _solve_multipliers(transfer)
    if multipliers is None:
        status = CASCADE_STATUS_FAILED
        diagnostics.append(_diagnostic(
            "cascade_solve_failed", year, "", "",
            "The conversion network could not be solved; converters are treated as final "
            "use for this year, as in the one-step method.",
        ))
        consumer_rows = rows.assign(role=CASCADE_ROLE_FINAL, passed_to_pool="")
        multipliers = np.eye(len(pools))

    consumer_rows = consumer_rows[consumer_rows["resource_pool"].isin(pools)]
    consumer_rows = consumer_rows.loc[:, list(_CONSUMER_COLUMNS)].reset_index(drop=True)
    final_rows = consumer_rows[consumer_rows["role"].eq(CASCADE_ROLE_FINAL)]
    sectors = tuple(sorted(final_rows["sector"].unique(), key=str))
    final_shares = np.zeros((len(pools), len(sectors)))
    if sectors:
        sector_index = {sector: position for position, sector in enumerate(sectors)}
        by_sector = final_rows.groupby(["resource_pool", "sector"])["share"].sum()
        for (pool, sector), share in by_sector.items():
            final_shares[index[pool], sector_index[sector]] += float(share)
    return CascadeNetwork(
        year=year,
        pools=tuple(pools),
        sectors=sectors,
        multipliers=multipliers,
        final_shares=final_shares,
        consumer_shares=consumer_rows,
        status=status,
    ), diagnostics


def origin_sector_shares(networks: dict[int, CascadeNetwork]) -> pd.DataFrame:
    """Return the share of one unit of a pool's own cost reaching each sector."""
    records: list[tuple[int, str, str, float]] = []
    for year, network in networks.items():
        if not network.pools or not network.sectors:
            continue
        reach = network.multipliers.T @ network.final_shares
        origins, sectors = np.nonzero(reach)
        for origin, sector in zip(origins, sectors):
            records.append((
                int(year), network.pools[origin], network.sectors[sector],
                float(reach[origin, sector]),
            ))
    return pd.DataFrame(records, columns=list(ORIGIN_SECTOR_SHARE_COLUMNS))


def unallocated_ledger_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Return ledger rows for cost that stays in a pool without a destination.

    ``frame`` holds ``period``, ``view``, ``sign``, ``resource_pool`` and
    ``allocated_cost_m``.
    """
    return frame.assign(
        region="", process="", sector="", role=CASCADE_ROLE_UNALLOCATED, passed_to_pool="",
    ).loc[:, list(PROCESS_ALLOCATION_COLUMNS)]


def apply_cascade(
    injections: pd.DataFrame,
    networks: dict[int, CascadeNetwork],
) -> CascadeOutcome:
    """Cascade own source cost through each share-basis year's network.

    ``injections`` holds ``period``, ``view``, ``sign``, ``share_basis_year``,
    ``resource_pool`` (the pool the cost was assigned to) and ``cost_m``.
    ``sign`` is carried through so costs and credits can be shown apart.
    Cost that cannot reach final use is a ``CASCADE_ROLE_UNALLOCATED`` row of
    the pool where it stops, so every pool's ledger balances.
    """
    process_parts: list[pd.DataFrame] = []
    origin_records: list[tuple] = []
    unallocated_records: list[tuple] = []
    group_keys = ["period", "view", "sign", "share_basis_year"]
    for (period, view, sign, year), group in injections.groupby(group_keys, sort=True):
        network = networks[int(year)]
        index = {pool: position for position, pool in enumerate(network.pools)}
        injected = np.zeros(len(network.pools))
        stuck: list[tuple[str, float]] = []
        for pool, value in group.groupby("resource_pool")["cost_m"].sum().items():
            if pool in index:
                injected[index[pool]] += float(value)
            else:
                stuck.append((pool, float(value)))
        if index:
            entering = pd.Series(network.multipliers @ injected, index=list(network.pools))
            shares = network.consumer_shares
            amounts = shares["share"] * shares["resource_pool"].map(entering)
            part = shares.loc[amounts.ne(0)].drop(columns="share").assign(
                allocated_cost_m=amounts[amounts.ne(0)], period=period, view=view, sign=sign,
            )
            process_parts.append(part)
            leaked = part[
                part["role"].eq(CASCADE_ROLE_PASSED_ON)
                & ~part["passed_to_pool"].isin(list(network.pools))
            ].groupby("passed_to_pool")["allocated_cost_m"].sum()
            stuck.extend((pool, float(value)) for pool, value in leaked.items())
        for pool, value in stuck:
            unallocated_records.append((period, view, pool, value))
        if stuck:
            process_parts.append(unallocated_ledger_rows(
                pd.DataFrame(stuck, columns=["resource_pool", "allocated_cost_m"]).assign(
                    period=period, view=view, sign=sign,
                )
            ))
        if not index:
            continue

        origins = np.nonzero(injected)[0]
        if len(origins) and network.sectors:
            reached = network.multipliers[:, origins] * injected[origins]
            by_sector = reached[:, :, None] * network.final_shares[:, None, :]
            for pool, origin, sector in zip(*np.nonzero(by_sector)):
                origin_records.append((
                    period, view, network.pools[origins[origin]], network.pools[pool],
                    network.sectors[sector], float(by_sector[pool, origin, sector]),
                ))

    if process_parts:
        process_allocations = pd.concat(process_parts, ignore_index=True).groupby(
            list(PROCESS_ALLOCATION_COLUMNS[:-1]), as_index=False, dropna=False, sort=True,
        )["allocated_cost_m"].sum()
    else:
        process_allocations = pd.DataFrame(columns=list(PROCESS_ALLOCATION_COLUMNS)).astype(
            {"allocated_cost_m": float}
        )
    cascade_allocations = pd.DataFrame(
        origin_records, columns=list(CASCADE_ALLOCATION_COLUMNS)
    ).astype({"allocated_cost_m": float}).groupby(
        list(CASCADE_ALLOCATION_COLUMNS[:-1]), as_index=False, sort=True,
    )["allocated_cost_m"].sum()
    cascade_unallocated = pd.DataFrame(
        unallocated_records, columns=list(CASCADE_UNALLOCATED_COLUMNS)
    ).astype({"cascade_unallocated_cost_m": float}).groupby(
        list(CASCADE_UNALLOCATED_COLUMNS[:-1]), as_index=False, sort=True,
    )["cascade_unallocated_cost_m"].sum()
    return CascadeOutcome(
        process_allocations=process_allocations.loc[:, list(PROCESS_ALLOCATION_COLUMNS)],
        cascade_allocations=cascade_allocations,
        cascade_unallocated=cascade_unallocated,
    )
