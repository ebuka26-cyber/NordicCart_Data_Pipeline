"""Opening inventory: how much of each product every store and warehouse holds on day one.

Demand model (all Decimal, seeded per product and per location, see rules.py):

  product weekly units   = category weekly_units
                           x (category reference price / product price) ** price_elasticity   (clipped)
                           x popularity (seeded per product)
  location weekly units  = product weekly units x market share x channel share
      market share         = customer country weights (config [customers])
      warehouse            = online_share (each market's warehouse serves its online orders)
      store                = (1 - online_share) x format weight / sum of format weights of the
                             market's stores open on the opening day (flagship 2, standard 1)
  opening quantity       = max(minimum, round(location weekly units x weeks of cover x noise))
      store minimum        = display units by price band (+1 in a flagship)
      warehouse minimum    = warehouse_minimum_units
  reorder point          = ceil(location weekly units x lead time x safety factor),
                           at least (store minimum - 1) in stores and 2 in warehouses,
                           and below the opening quantity

A listing is stocked only if the product is on sale on the opening day (launched on or
before it, active) at a location that is open and active. Otherwise it is listed at 0.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, localcontext

from ..master_data.model import MasterDataset
from ..master_data.rules import local_midnight_utc, stream, uniform_decimal
from ..settings import Settings
from .config import InventorySettings
from .model import OpeningInventory, OpeningListing

UNITS = Decimal("0.0001")


def _price_eur(master: MasterDataset, settings: Settings) -> dict[str, Decimal]:
    """Opening EUR price per product: the EUR market price, else a converted price."""
    prices: dict[str, Decimal] = {}
    for p in master.product_prices:
        if p.valid_to is None and p.currency_code == "EUR":
            prices[p.sku] = p.list_price
    for p in master.product_prices:
        if p.sku not in prices and p.valid_to is None:
            prices[p.sku] = p.list_price / settings.pricing.reference_rates[p.currency_code]
    return prices


def _power(base: Decimal, exponent: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 28
        return base ** exponent


def display_minimum(inv: InventorySettings, price_eur: Decimal, store_format: str) -> int:
    if price_eur < 100:
        units = inv.store_display_units["under_100_eur"]
    elif price_eur < 500:
        units = inv.store_display_units["under_500_eur"]
    else:
        units = inv.store_display_units["from_500_eur"]
    return units + (inv.flagship_extra_units if store_format == "flagship" else 0)


def generate_opening_inventory(settings: Settings, inv: InventorySettings, master: MasterDataset) -> OpeningInventory:
    countries = {c.country_code: c for c in master.countries}
    store_format = {s.location_code: s.store_format for s in master.stores}
    ships_online = {w.location_code: w.ships_online_orders for w in master.warehouses}
    weights = {k: v for k, v in settings.customer.country_weights.items() if k in countries}
    total_weight = sum(weights.values())
    market_share = {k: v / total_weight for k, v in weights.items()}

    def available(loc) -> bool:
        return loc.is_active and loc.opened_on <= inv.opening_date

    # Channel share of each location within its market.
    channel_share: dict[str, Decimal] = {}
    for code in countries:
        stores = [l for l in master.stock_locations if l.country_code == code and l.location_type == "store" and available(l)]
        online = [l for l in master.stock_locations if l.country_code == code and l.location_type == "warehouse"
                  and available(l) and ships_online.get(l.location_code)]
        store_weight_sum = sum(inv.store_format_weight[store_format[s.location_code]] for s in stores)
        for s in stores:
            channel_share[s.location_code] = (1 - inv.online_share) * inv.store_format_weight[store_format[s.location_code]] / store_weight_sum
        for w in online:
            channel_share[w.location_code] = inv.online_share / len(online)

    prices = _price_eur(master, settings)
    low_factor, high_factor = inv.price_factor_range
    listings: list[OpeningListing] = []
    for product in master.products:
        demand = inv.category_demand[product.category_code]
        price = prices[product.sku]
        factor = _power(demand.reference_price_eur / price, inv.price_elasticity)
        factor = min(max(factor, low_factor), high_factor)
        popularity = uniform_decimal(stream(settings.seed, "inventory", "demand", product.sku), *inv.popularity_range, steps=10_000)
        product_weekly = demand.weekly_units * factor * popularity
        on_sale = product.is_active and product.launched_on is not None and product.launched_on <= inv.opening_date

        for loc in master.stock_locations:
            share = market_share.get(loc.country_code, Decimal(0)) * channel_share.get(loc.location_code, Decimal(0))
            weekly = (product_weekly * share).quantize(UNITS, rounding=ROUND_HALF_UP)
            kind = loc.location_type
            if kind == "store":
                minimum = display_minimum(inv, price, store_format[loc.location_code])
                reorder_floor = minimum - 1
            else:
                minimum = inv.warehouse_minimum_units
                reorder_floor = 2
            noise = uniform_decimal(stream(settings.seed, "inventory", "opening", product.sku, loc.location_code),
                                    *inv.location_noise_range, steps=10_000)
            target = weekly * inv.weeks_of_cover[kind] * noise
            reorder = max(int((weekly * inv.lead_time_weeks[kind] * inv.safety_factor).to_integral_value(ROUND_CEILING)),
                          reorder_floor)
            stocked = on_sale and available(loc)
            if stocked:
                quantity = max(minimum, int(target.to_integral_value(ROUND_HALF_UP)))
                reorder = min(reorder, quantity - 1)
            else:
                quantity = 0
            listings.append(OpeningListing(
                location_code=loc.location_code,
                location_type=kind,
                country_code=loc.country_code,
                sku=product.sku,
                category_code=product.category_code,
                price_eur=price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                expected_weekly_units=weekly,
                reorder_point=reorder,
                opening_quantity=quantity,
                occurred_at=local_midnight_utc(inv.opening_date, countries[loc.country_code].time_zone) if stocked else None,
                external_reference=inv.external_reference if stocked else None,
            ))
    return OpeningInventory(profile=settings.profile, seed=settings.seed, opening_date=inv.opening_date, listings=listings)
