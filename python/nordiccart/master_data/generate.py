"""Builds the NordicCart master dataset from the settings and the reference data.

Fixed by reference data (identical in every profile):
  4 currencies, 4 countries, 10 product categories, 12 stores, 4 warehouses.
Generated from the seed (size set by the profile):
  products, one opening list price per product and market, customers.

Product n and customer n always come out the same for a given seed, whatever the
profile size, because each draws from its own random stream. The pilot rows are
therefore exactly the first rows of the full dataset.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..settings import Settings
from .model import (
    Country, Currency, Customer, MasterDataset, Product, ProductCategory, ProductPrice,
    StockLocation, Store, Warehouse,
)
from .reference import Category, Market, Reference, load_reference
from .rules import (
    ONE, add_days, email_part, floor_int, local_midnight_utc, local_to_utc,
    log_uniform_decimal, money, net_of_vat, power_fraction, price_point, stream,
    uniform_decimal, years_before,
)

# Local hour of online sign-ups (evening peak) and age at registration.
ONLINE_SIGNUP_HOUR_WEIGHTS = (1, 1, 1, 1, 1, 1, 2, 4, 6, 8, 8, 8, 10, 10, 8, 8, 8, 10, 12, 14, 16, 16, 12, 6)
STORE_SIGNUP_HOURS = range(10, 20)                  # store opening hours, local time
AGE_BANDS = ((18, 24, 8), (25, 34, 12), (35, 44, 11), (45, 54, 9), (55, 64, 6), (65, 80, 3))
EMAIL_PATTERNS = ("{f}.{l}", "{f}.{l}", "{f}{l}", "{f}_{l}", "{fi}.{l}", "{f}.{l}{yy}", "{f}{n}")


@dataclass(frozen=True)
class _ProductPlan:
    product: Product
    category: Category
    base_price_eur: Decimal
    margin: Decimal


def generate_master_data(settings: Settings, reference: Reference | None = None) -> MasterDataset:
    ref = reference or load_reference()
    currencies = [Currency(c["currency_code"], c["currency_name"], int(c["minor_units"])) for c in ref.currencies]
    countries = [Country(m.country_code, m.country_name, m.currency_code, m.vat_rate, m.time_zone) for m in ref.markets]
    categories = [ProductCategory(c.category_code, c.category_name, True) for c in ref.categories]

    locations, stores, warehouses = _locations(ref)
    plans = _products(settings, ref)
    prices = _prices(settings, ref, plans)
    customers = _customers(settings, ref)

    return MasterDataset(
        profile=settings.profile,
        seed=settings.seed,
        currencies=currencies,
        countries=countries,
        product_categories=categories,
        products=[p.product for p in plans],
        product_prices=prices,
        stock_locations=locations,
        stores=stores,
        warehouses=warehouses,
        customers=customers,
    )


# --------------------------------------------------------------------------- locations
def _locations(ref: Reference):
    locations, stores, warehouses = [], [], []
    for spec in ref.warehouses + ref.stores:
        locations.append(StockLocation(
            location_code=spec.location_code, location_name=spec.location_name, location_type=spec.location_type,
            country_code=spec.country_code, address_line1=spec.address_line1, postal_code=spec.postal_code,
            city=spec.city, opened_on=spec.opened_on, is_active=True,
        ))
        if spec.location_type == "store":
            stores.append(Store(spec.location_code, spec.store_format))
        else:
            warehouses.append(Warehouse(spec.location_code, bool(spec.ships_online_orders), spec.storage_capacity_units))
    return locations, stores, warehouses


# --------------------------------------------------------------------------- products
def _render_name(rng: random.Random, pattern: str, brand: str, parts: dict[str, tuple[str, ...]]) -> str:
    values = {key: rng.choice(options) for key, options in sorted(parts.items())}
    return pattern.format(brand=brand, **values)


def _products(settings: Settings, ref: Reference) -> list[_ProductPlan]:
    categories = ref.categories
    weights = [c.weight for c in categories]
    window_days = (settings.simulation_start - settings.catalogue_start).days
    late_days = (settings.simulation_end - settings.simulation_start).days - 60
    plans: list[_ProductPlan] = []
    used_names: set[str] = set()
    for n in range(1, settings.products + 1):
        rng = stream(settings.seed, "product", n)
        # Products 1-10 cover every category once; later ones follow the category weights.
        category = categories[n - 1] if n <= len(categories) else rng.choices(categories, weights=weights)[0]
        brand = rng.choice(category.brands)
        ptype = rng.choice(category.types)
        name = _render_name(rng, ptype.pattern, brand, ptype.parts)
        attempts = 0
        while name.lower() in used_names and attempts < 25:
            brand = rng.choice(category.brands)
            name = _render_name(rng, ptype.pattern, brand, ptype.parts)
            attempts += 1
        if name.lower() in used_names:
            name = f"{name} ({n})"
        used_names.add(name.lower())

        if rng.random() < float(settings.launched_before_simulation_share):
            launched_on = add_days(settings.catalogue_start, rng.randrange(window_days))
        else:
            launched_on = add_days(settings.simulation_start, rng.randrange(max(late_days, 1)))

        plans.append(_ProductPlan(
            product=Product(
                sku=f"{category.sku_prefix}-{n:06d}",
                product_name=name,
                brand=brand,
                category_code=category.category_code,
                launched_on=launched_on,
                discontinued_on=None,
                is_active=True,
            ),
            category=category,
            base_price_eur=log_uniform_decimal(rng, *ptype.price_eur),
            margin=uniform_decimal(rng, *category.margin, steps=10_000),
        ))
    return plans


def _prices(settings: Settings, ref: Reference, plans: list[_ProductPlan]) -> list[ProductPrice]:
    """One opening list price per product and market, valid from launch (local midnight), open-ended."""
    prices: list[ProductPrice] = []
    jitter = settings.pricing.margin_jitter
    for plan in plans:
        for market in ref.markets:
            rng = stream(settings.seed, "price", plan.product.sku, market.country_code)
            rate = settings.pricing.reference_rates[market.currency_code]
            factor = settings.pricing.market_price_factor[market.country_code]
            list_price = price_point(plan.base_price_eur * rate * factor, market.currency_code)
            margin = plan.margin + uniform_decimal(rng, -jitter, jitter, steps=10_000)
            margin = min(max(margin, Decimal("0.05")), Decimal("0.80"))
            unit_cost = money(net_of_vat(list_price, market.vat_rate) * (ONE - margin))
            prices.append(ProductPrice(
                sku=plan.product.sku,
                country_code=market.country_code,
                currency_code=market.currency_code,
                list_price=list_price,
                unit_cost=unit_cost,
                valid_from=local_midnight_utc(plan.product.launched_on, market.time_zone),
                valid_to=None,
            ))
    return prices


# --------------------------------------------------------------------------- customers
def _age_at_signup(rng: random.Random) -> int:
    ages, weights = [], []
    for low, high, weight in AGE_BANDS:
        for age in range(low, high + 1):
            ages.append(age)
            weights.append(weight)
    return rng.choices(ages, weights=weights)[0]


def _postal_code(rng: random.Random, market: Market, city_name: str) -> str:
    city = market.city(city_name)
    digits = market.postal_code_format.count("N")
    code = f"{rng.randint(city.postal_min, city.postal_max):0{digits}d}"
    if market.postal_code_format == "NNN NN":
        code = f"{code[:3]} {code[3:]}"
    return code


def _phone(rng: random.Random, market: Market) -> str:
    """Mobile number in the market's usual layout; every {dN} placeholder gets fresh digits."""
    def fill(match: re.Match) -> str:
        token = match.group(1)
        if token == "prefix":
            return market.phone_prefix
        if token == "mobile":
            return rng.choice(market.mobile_prefixes)
        return "".join(str(rng.randrange(10)) for _ in range(int(token[1:])))
    return re.sub(r"\{(prefix|mobile|d[1-9])\}", fill, market.phone_layout)


def _email(rng: random.Random, first: str, last: str, birth: date | None,
           domains: tuple[str, ...], used: set[str]) -> str:
    f, l = email_part(first), email_part(last)
    yy = f"{birth.year % 100:02d}" if birth else f"{rng.randrange(100):02d}"
    local = rng.choice(EMAIL_PATTERNS).format(f=f, l=l, fi=f[:1], yy=yy, n=rng.randint(1, 99))
    domain = rng.choice(domains)
    email = f"{local}@{domain}"
    while email in used:                       # unique among earlier customers
        email = f"{local}{rng.randint(2, 999)}@{domain}"
    used.add(email)
    return email


def _customers(settings: Settings, ref: Reference) -> list[Customer]:
    cs = settings.customer
    market_codes = [m.country_code for m in ref.markets if cs.country_weights.get(m.country_code, 0) > 0]
    weights = [float(cs.country_weights[c]) for c in market_codes]
    inverse_growth = ONE / cs.growth_exponent
    stores_by_country: dict[str, list] = {}
    for s in ref.stores:
        stores_by_country.setdefault(s.country_code, []).append(s)

    used_emails: set[str] = set()
    customers: list[Customer] = []
    for n in range(1, settings.customers + 1):
        rng = stream(settings.seed, "customer", n)
        market = ref.market(rng.choices(market_codes, weights=weights)[0])

        # Registration day: between the market launch and the last simulated day, more
        # sign-ups in recent years (cumulative share of sign-ups by time t is t ** growth).
        launch = market.market_launched_on
        span_days = (settings.simulation_end - launch).days + 1
        q = Decimal(rng.randrange(1, 1_000_000)) / Decimal(1_000_000)
        offset = min(floor_int(power_fraction(q, inverse_growth) * span_days), span_days - 1)
        reg_day = add_days(launch, offset)

        open_stores = [s for s in stores_by_country.get(market.country_code, []) if s.opened_on <= reg_day]
        in_store = bool(open_stores) and rng.random() < float(cs.in_store_signup_share)
        if in_store:
            store = rng.choices(open_stores, weights=[2 if s.store_format == "flagship" else 1 for s in open_stores])[0]
            hour = rng.choice(STORE_SIGNUP_HOURS)
        else:
            store = None
            hour = rng.choices(range(24), weights=ONLINE_SIGNUP_HOUR_WEIGHTS)[0]
        registered_at = local_to_utc(reg_day, hour, rng.randrange(60), rng.randrange(60), market.time_zone)

        female = rng.random() < 0.5
        first = rng.choice(ref.names[market.country_code]["female" if female else "male"])
        last = rng.choice(ref.names[market.country_code]["last"])

        birth = None
        if rng.random() < float(cs.birth_date_share):
            age = _age_at_signup(rng)
            latest = years_before(reg_day, age)                       # turns `age` on reg_day
            birth = add_days(latest, -rng.randrange(365))             # still `age` on reg_day

        address = postal = city = None
        if rng.random() < float(cs.address_share):
            if store is not None and rng.random() < float(cs.in_store_home_city_share):
                city = store.city
            else:
                city = rng.choices([c.city for c in market.cities], weights=[c.weight for c in market.cities])[0]
            address = f"{rng.choice(market.streets)} {rng.randint(1, 120)}{rng.choice(market.address_suffixes)}"
            postal = _postal_code(rng, market, city)

        phone = _phone(rng, market) if rng.random() < float(cs.phone_share) else None
        email = _email(rng, first, last, birth, cs.email_domains, used_emails)

        customers.append(Customer(
            customer_number=f"C{n:08d}",
            first_name=first,
            last_name=last,
            email=email,
            phone=phone,
            birth_date=birth,
            address_line1=address,
            postal_code=postal,
            city=city,
            country_code=market.country_code,
            signup_channel="in_store" if in_store else "online",
            registered_at=registered_at,
            is_active=True,
        ))
    return customers


def describe(dataset: MasterDataset, settings: Settings) -> dict:
    """Small summary for the log: customers per market, sign-up channel, launches in the window."""
    per_market: dict[str, int] = {}
    for c in dataset.customers:
        per_market[c.country_code] = per_market.get(c.country_code, 0) + 1
    registered_in_window = sum(
        1 for c in dataset.customers if c.registered_at.date() >= settings.simulation_start)
    return {
        "customers_per_market": dict(sorted(per_market.items())),
        "in_store_signups": sum(1 for c in dataset.customers if c.signup_channel == "in_store"),
        "customers_registered_in_simulation_window": registered_in_window,
        "products_launched_in_simulation_window": sum(
            1 for p in dataset.products if p.launched_on and p.launched_on >= settings.simulation_start),
    }
