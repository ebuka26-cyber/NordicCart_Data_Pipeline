"""Settings for the NordicCart generators.

Two sources, kept apart on purpose:
  * config/generator.toml: seed, dataset sizes, calendar, distributions. No secrets.
  * Database credentials: the standard PostgreSQL variables (PGHOST, PGPORT,
    PGDATABASE, PGUSER, PGPASSWORD), read from the environment or from config/.env.
    config/.env is ignored by git and is never logged.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "generator.toml"
DEFAULT_ENV_FILE = REPO_ROOT / "config" / ".env"
PG_ENV_KEYS = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGPASSFILE", "PGSSLMODE")


class SettingsError(ValueError):
    """The configuration file is missing a value or has an invalid one."""


@dataclass(frozen=True)
class CustomerSettings:
    country_weights: dict[str, Decimal]
    growth_exponent: Decimal
    in_store_signup_share: Decimal
    in_store_home_city_share: Decimal
    phone_share: Decimal
    birth_date_share: Decimal
    address_share: Decimal
    email_domains: tuple[str, ...]


@dataclass(frozen=True)
class PricingSettings:
    reference_rates: dict[str, Decimal]
    market_price_factor: dict[str, Decimal]
    margin_jitter: Decimal


@dataclass(frozen=True)
class Settings:
    config_path: Path
    seed: int
    profile: str
    products: int
    customers: int
    simulation_start: date
    simulation_end: date
    catalogue_start: date
    customer: CustomerSettings
    launched_before_simulation_share: Decimal
    pricing: PricingSettings
    csv_dir: Path
    log_dir: Path
    generation_end: date | None = None   # S10: [demand] generation_end, last day of synthetic activity (default simulation_end)

    @property
    def activity_end(self) -> date:
        """Last day the activity engine may generate (demand calendar, exchange rates)."""
        return self.generation_end or self.simulation_end


def _require(table: dict, key: str, where: str):
    if key not in table:
        raise SettingsError(f"Missing '{key}' in [{where}] of the generator config.")
    return table[key]


def _share(value, name: str) -> Decimal:
    value = Decimal(str(value))
    if not Decimal(0) <= value <= Decimal(1):
        raise SettingsError(f"{name} must be between 0 and 1, got {value}.")
    return value


def load_settings(config_path: Path | str | None = None, profile: str | None = None) -> Settings:
    """Read config/generator.toml (or the given file) and select a profile."""
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    if not path.is_file():
        raise SettingsError(f"Config file not found: {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh, parse_float=Decimal)

    gen = _require(raw, "generator", "root")
    seed = _require(gen, "seed", "generator")
    if not isinstance(seed, int) or seed < 0:
        raise SettingsError("generator.seed must be a non-negative integer.")
    profile = profile or _require(gen, "default_profile", "generator")
    profiles = _require(raw, "profiles", "root")
    if profile not in profiles:
        raise SettingsError(f"Unknown profile '{profile}'. Available: {', '.join(sorted(profiles))}.")
    sizes = profiles[profile]
    products = int(_require(sizes, "products", f"profiles.{profile}"))
    customers = int(_require(sizes, "customers", f"profiles.{profile}"))
    if products < 10:
        raise SettingsError("A profile needs at least 10 products (one per category).")
    if customers < 1:
        raise SettingsError("A profile needs at least 1 customer.")

    cal = _require(raw, "calendar", "root")
    sim_start = _require(cal, "simulation_start", "calendar")
    sim_end = _require(cal, "simulation_end", "calendar")
    cat_start = _require(cal, "catalogue_start", "calendar")
    for name, value in (("simulation_start", sim_start), ("simulation_end", sim_end), ("catalogue_start", cat_start)):
        if not isinstance(value, date):
            raise SettingsError(f"calendar.{name} must be a date (YYYY-MM-DD without quotes).")
    if not cat_start < sim_start <= sim_end:
        raise SettingsError("calendar dates must satisfy catalogue_start < simulation_start <= simulation_end.")
    if sim_end >= date.today():
        raise SettingsError("calendar.simulation_end must be in the past (registrations and launches cannot be in the future).")

    cust = _require(raw, "customers", "root")
    weights = {str(k): Decimal(str(v)) for k, v in _require(cust, "country_weights", "customers").items()}
    if any(w < 0 for w in weights.values()) or sum(weights.values()) <= 0:
        raise SettingsError("customers.country_weights must be non-negative and not all zero.")
    customer = CustomerSettings(
        country_weights=weights,
        growth_exponent=Decimal(str(_require(cust, "growth_exponent", "customers"))),
        in_store_signup_share=_share(_require(cust, "in_store_signup_share", "customers"), "in_store_signup_share"),
        in_store_home_city_share=_share(_require(cust, "in_store_home_city_share", "customers"), "in_store_home_city_share"),
        phone_share=_share(_require(cust, "phone_share", "customers"), "phone_share"),
        birth_date_share=_share(_require(cust, "birth_date_share", "customers"), "birth_date_share"),
        address_share=_share(_require(cust, "address_share", "customers"), "address_share"),
        email_domains=tuple(_require(cust, "email_domains", "customers")),
    )
    if customer.growth_exponent <= 0:
        raise SettingsError("customers.growth_exponent must be positive.")
    if not customer.email_domains:
        raise SettingsError("customers.email_domains must list at least one domain.")

    prod = _require(raw, "products", "root")
    pricing_raw = _require(raw, "pricing", "root")
    pricing = PricingSettings(
        reference_rates={str(k): Decimal(str(v)) for k, v in _require(pricing_raw, "reference_rates", "pricing").items()},
        market_price_factor={str(k): Decimal(str(v)) for k, v in _require(pricing_raw, "market_price_factor", "pricing").items()},
        margin_jitter=Decimal(str(_require(pricing_raw, "margin_jitter", "pricing"))),
    )
    if any(v <= 0 for v in pricing.reference_rates.values()) or any(v <= 0 for v in pricing.market_price_factor.values()):
        raise SettingsError("pricing rates and factors must be positive.")

    generation_end = raw.get("demand", {}).get("generation_end", sim_end)
    if not isinstance(generation_end, date) or generation_end < sim_end \
            or (generation_end + timedelta(days=1)).day != 1:
        raise SettingsError("demand.generation_end must be a month end on or after calendar.simulation_end.")

    out = raw.get("output", {})
    return Settings(
        generation_end=generation_end,
        config_path=path,
        seed=seed,
        profile=profile,
        products=products,
        customers=customers,
        simulation_start=sim_start,
        simulation_end=sim_end,
        catalogue_start=cat_start,
        customer=customer,
        launched_before_simulation_share=_share(
            _require(prod, "launched_before_simulation_share", "products"), "launched_before_simulation_share"),
        pricing=pricing,
        csv_dir=REPO_ROOT / out.get("csv_dir", "output/master_data"),
        log_dir=REPO_ROOT / out.get("log_dir", "logs"),
    )


def load_env_file(path: Path | str | None = None) -> list[str]:
    """Load KEY=VALUE lines from config/.env into os.environ.

    Variables that are already set in the environment win, so a value exported in
    the terminal overrides the file. Returns the names (never the values) loaded.
    Lines starting with # and blank lines are ignored; values may be quoted.
    """
    path = Path(path) if path else DEFAULT_ENV_FILE
    loaded: list[str] = []
    if not path.is_file():
        return loaded
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def describe_connection_target() -> str:
    """Host, port, database and user from the environment. Never the password."""
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    db = os.environ.get("PGDATABASE", os.environ.get("PGUSER", "(default)"))
    user = os.environ.get("PGUSER", "(current OS user)")
    return f"{user}@{host}:{port}/{db}"
