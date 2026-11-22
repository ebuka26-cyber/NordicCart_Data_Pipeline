# config

Configuration templates. Real credentials never go into the repository.

| File | Purpose |
|---|---|
| `.env.example` | Template for PostgreSQL connection settings (standard libpq variable names). Copy to `config/.env` and fill in. |
| `generator.toml` | Synthetic data settings, no secrets: seed, profile sizes (pilot: 20 products, 100 customers; full: 500 and 20,000), simulation window (2024-11-01 to 2026-10-31), customer mix per market, price conversion rates; `[inventory]`: opening date and reference, demand per category, online share, store format weights, weeks of cover, lead times, display and warehouse minimums; `[transactions]`: pilot order window, scenario mix (20 orders), channel share, lines per order, online and store hours, reservation lifetimes, payment methods and providers, maximum attempts, failure reasons, shipping fees and free-shipping thresholds; `[replenishment]`: replenishment pilot window (2024-11-15 to 2024-12-06, after the pilot's last event), review schedule (stores every evening Monday to Saturday, warehouses on Mondays), R0 hour, warehouse working days, dispatch and receiving hours, transit minutes, supplier lead time, split-delivery gap, pre-launch days, echelon reorder points, warehouse protection, case packs per category, and the pilot scenarios (one forced purchase order per market, the SE one split into two delivery notes). |

`config/.env` is listed in `.gitignore`. The SQL scripts in this project contain no credentials: Valentina Studio and psql ask for the password when connecting.

The Python tools read `config/.env` at start-up; a variable already set in the terminal wins over the file. Only the variable names are logged, never the values.
