"""Inventory uses of the demand model (design 6): one demand model, three uses.

    opening_comparison      the current opening model's weekly units (Phase 3 step 2, unchanged)
                            against the demand model's expected fulfilled units, per category
                            and location type, for reference weeks
    recalibrated_opening    opening quantity and reorder point per listing with the opening
                            model's rules unchanged (weeks of cover, display and warehouse
                            minimums, the same seeded noise) but weekly units from the demand
                            model: the basis of the opening stock of products added in S7 (E2)
    rcal_reorder_points     the monthly review R-cal (S6): stores forecast demand over their lead
                            time x safety factor (at least display minimum - 1); warehouses echelon
                            demand (online + the market's stores) over their lead time x safety
                            factor (at least 2). The forecast window follows the season, which is
                            the pre-build of design 6; the capacity guard needs stock and stays in S6.
    rcal_cover_units        S8.1: the same forecast over the weeks of cover (stores 6, warehouses
                            echelon 10): the engine's order-up-to level S after each R-cal.
Nothing here writes to a database. The 20 pilot products keep their loaded opening balances.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, localcontext

from ..inventory.generate import display_minimum
from ..inventory.model import OpeningInventory
from ..master_data.rules import stream, uniform_decimal
from .model import DemandModel
from .numeric import PREC

ZERO = Decimal(0)


def _ceil(x: Decimal) -> int:
    return int(x.to_integral_value(ROUND_CEILING))


def period_weekly_units(model: DemandModel, first: date, last: date) -> dict[tuple[str, str], Decimal]:
    """Expected fulfilled units per listing, as an average week of first..last."""
    days = Decimal((last - first).days + 1)
    with localcontext() as ctx:
        ctx.prec = PREC
        return {k: v * 7 / days for k, v in model.expected_units(first, last).items()}


def opening_comparison(model: DemandModel, inventory: OpeningInventory,
                       weeks: dict[str, dict[tuple[str, str], Decimal]]) -> dict:
    """Old (opening model) versus new (demand model) weekly units on the stocked opening listings."""
    stocked = [x for x in inventory.listings if x.opening_quantity > 0]
    out = {"stocked_listings": len(stocked), "opening_units": sum(x.opening_quantity for x in stocked),
           "old_weekly_units": sum(x.expected_weekly_units for x in stocked), "weeks": {}}
    for name, new in weeks.items():
        by_category = defaultdict(lambda: [ZERO, ZERO])
        by_type = defaultdict(lambda: [ZERO, ZERO])
        for x in stocked:
            value = new.get((x.location_code, x.sku), ZERO)
            for table, key in ((by_category, x.category_code), (by_type, x.location_type)):
                table[key][0] += x.expected_weekly_units
                table[key][1] += value
        new_total = sum(new.get((x.location_code, x.sku), ZERO) for x in stocked)
        out["weeks"][name] = {
            "new_weekly_units": new_total,
            "old_over_new": out["old_weekly_units"] / new_total if new_total else None,
            "opening_weeks_of_cover": out["opening_units"] / new_total if new_total else None,
            "by_category": {k: {"old": v[0], "new": v[1], "old_over_new": v[0] / v[1] if v[1] else None}
                            for k, v in sorted(by_category.items())},
            "by_location_type": {k: {"old": v[0], "new": v[1], "old_over_new": v[0] / v[1] if v[1] else None}
                                 for k, v in sorted(by_type.items())},
        }
    return out


def recalibrated_opening(model: DemandModel, inventory: OpeningInventory,
                         weekly: dict[tuple[str, str], Decimal]) -> dict[tuple[str, str], tuple[int, int]]:
    """(opening quantity, reorder point) per listing on sale at the opening, opening rules unchanged."""
    inv = model.ds.inventory
    out = {}
    with localcontext() as ctx:
        ctx.prec = PREC
        for x in inventory.listings:
            if x.occurred_at is None:              # not on sale at an open location on the opening day
                continue
            kind = x.location_type
            units = weekly.get((x.location_code, x.sku), ZERO).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if kind == "store":                    # unrounded EUR price, as in the opening model
                minimum = display_minimum(inv, model.products[x.sku].price_eur, model.store_format[x.location_code])
                floor = minimum - 1
            else:
                minimum, floor = inv.warehouse_minimum_units, 2
            noise = uniform_decimal(stream(model.catalogue_seed, "inventory", "opening", x.sku, x.location_code),
                                    *inv.location_noise_range, steps=10_000)
            quantity = max(minimum, int((units * inv.weeks_of_cover[kind] * noise).to_integral_value(ROUND_HALF_UP)))
            reorder = min(max(_ceil(units * inv.lead_time_weeks[kind] * inv.safety_factor), floor), quantity - 1)
            out[(x.location_code, x.sku)] = (quantity, reorder)
    return out


def opening_inventory_for(settings, inv, master, model: DemandModel | None = None) -> OpeningInventory:
    """The opening inventory of a profile (S7, design 6 and 10.1, decisions C2 and C3).

    pilot  the opening model, unchanged (generate_opening_inventory): what the pilots loaded.
    full   the same listings, of which:
             * the pilot products' listings stay exactly as the opening model gives them (they are
               loaded, and the pilots verify them);
             * every other product on sale on the opening day gets the demand-calibrated opening
               quantity and reorder point (recalibrated_opening with the expected units of the
               first combined week, demand.generation_start); exception E2 loads them later as
               opening balances dated at the opening count;
             * a product launched later is listed at 0 with a demand-model reorder point.
           The planning figure expected_weekly_units of a non-pilot listing is the demand model's:
           the first combined week, or for a later launch the first week at full attractiveness
           (launch + launch_ramp_weeks); the engine derives its order-up-to levels from it.
    """
    from dataclasses import replace

    from ..inventory.generate import generate_opening_inventory
    from ..master_data.generate import generate_master_data
    from ..settings import load_settings

    base = generate_opening_inventory(settings, inv, master)
    if settings.profile == "pilot":
        return base
    if model is None:
        raise ValueError("the full-profile opening inventory needs the demand model")
    pilot_skus = {p.sku for p in generate_master_data(load_settings(settings.config_path, "pilot")).products}
    first_week = model.weekly_units(model.ds.generation_start)
    recalibrated = recalibrated_opening(model, base, first_week)
    later_weeks: dict[date, dict] = {}
    ramp = timedelta(weeks=model.ds.attractiveness.launch_ramp_weeks)
    listings = []
    with localcontext() as ctx:
        ctx.prec = PREC
        for x in base.listings:
            if x.sku in pilot_skus:
                listings.append(x)
                continue
            if x.stocked:
                quantity, reorder = recalibrated[x.key]
                weekly = first_week.get(x.key, ZERO)
            else:
                launch = model.products[x.sku].launched_on
                day = max(launch + ramp, model.ds.generation_start) if launch else model.ds.generation_start
                if day not in later_weeks:
                    later_weeks[day] = model.weekly_units(day)
                weekly = later_weeks[day].get(x.key, ZERO)
                kind = x.location_type
                floor = (display_minimum(inv, model.products[x.sku].price_eur, model.store_format[x.location_code]) - 1
                         if kind == "store" else 2)
                quantity, reorder = 0, max(_ceil(weekly * inv.lead_time_weeks[kind] * inv.safety_factor), floor)
            listings.append(replace(x, expected_weekly_units=weekly.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
                                    opening_quantity=quantity, reorder_point=reorder))
    return OpeningInventory(profile=settings.profile, seed=settings.seed, opening_date=base.opening_date, listings=listings)


def forecast_limit(model: DemandModel, review_day: date) -> date:
    """S10: the last day a review on `review_day` can forecast. A review on or before the original calendar end
    (calendar.simulation_end) sees demand only up to it, exactly as when it ran; a later review sees the extension."""
    planning_end = model.ds.planning_end or model.calendar.end
    return planning_end if review_day <= planning_end else model.calendar.end


def rcal_reorder_points(model: DemandModel, review_day: date) -> dict[tuple[str, str], int]:
    """Reorder points of every listing of products on sale at a monthly review (R-cal, design 6)."""
    inv = model.ds.inventory
    out = {}
    with localcontext() as ctx:
        ctx.prec = PREC
        limit = forecast_limit(model, review_day)
        horizon = {kind: min(review_day + timedelta(days=int(inv.lead_time_weeks[kind] * 7) - 1), limit)
                   for kind in ("store", "warehouse")}
        store_demand = model.expected_units(review_day, horizon["store"],
                                            [l for l in model.locations if model.location_type[l] == "store"])
        echelon_all = model.expected_units(review_day, horizon["warehouse"])
        echelon: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        for (loc, sku), v in echelon_all.items():
            echelon[(model.warehouse[model.location_market[loc]], sku)] += v
        for (loc, sku), v in store_demand.items():
            price = model.products[sku].price_eur
            floor = display_minimum(inv, price, model.store_format[loc]) - 1
            out[(loc, sku)] = max(_ceil(v * inv.safety_factor), floor)
        for (loc, sku), v in echelon.items():
            out[(loc, sku)] = max(_ceil(v * inv.safety_factor), 2)
    return out


def rcal_cover_units(model: DemandModel, review_day: date) -> dict[tuple[str, str], Decimal]:
    """Forecast units over the weeks of cover from a monthly review (S8.1: the basis of S after R-cal).

    The same forecast as rcal_reorder_points, over weeks_of_cover instead of the lead time:
    stores their own expected fulfilled units over [review_day, + weeks_of_cover store); warehouses
    echelon units (online + the market's stores) over [review_day, + weeks_of_cover warehouse).
    The window follows the season, so S pre-builds for Black Week and December and falls after
    them. A listing without demand in the window (not launched yet, or discontinued) is absent.
    """
    inv = model.ds.inventory
    with localcontext() as ctx:
        ctx.prec = PREC
        limit = forecast_limit(model, review_day)
        horizon = {kind: min(review_day + timedelta(days=int(inv.weeks_of_cover[kind] * 7) - 1), limit)
                   for kind in ("store", "warehouse")}
        out = dict(model.expected_units(review_day, horizon["store"],
                                        [l for l in model.locations if model.location_type[l] == "store"]))
        echelon: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        for (loc, sku), v in model.expected_units(review_day, horizon["warehouse"]).items():
            echelon[(model.warehouse[model.location_market[loc]], sku)] += v
        out.update(echelon)
    return out
