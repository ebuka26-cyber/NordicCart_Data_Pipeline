# Python 3.14 compatibility report

Date: 2026-11-17. Scope: every Python file, dependency, configuration file and test in `NordicCart_Data_Pipeline`.

## Result

**Compatible with Python 3.14 without changes.** No Python file was modified. `requirements.txt` is unchanged. Migrations V001 to V007 and the configuration were not touched. Nothing was run against your PostgreSQL database.

## Environment

| Item | Version |
|---|---|
| Python | **CPython 3.14.6**, standard (GIL) build, Linux x86_64, isolated virtual environment |
| psycopg | 3.3.9, binary implementation, bundled libpq 18.6 |
| pytest | 9.2.0 |
| tzdata | 2026.6 |
| PostgreSQL | 16.15, disposable sandbox server, deleted afterwards |

Your laptop runs Python 3.14.7. The sandbox could not obtain that exact build: python.org and GitHub source downloads are blocked by its network policy, and the newest 3.14 build available was 3.14.6. A 3.14 bugfix release does not change the language or library interfaces. The section "Confirm on your laptop" shows a five-minute check on 3.14.7 that does not touch any database.

## What was inspected

| Area | Files |
|---|---|
| Package code | `python/nordiccart/__init__.py`, `settings.py`, `logging_setup.py`; `python/nordiccart/master_data/` `__init__`, `__main__`, `cli`, `export`, `generate`, `load`, `model`, `reference`, `rules`, `schema`, `validate` |
| Entry point | `python/run_master_data_seed.py` |
| Tests | `tests/python/conftest.py`, `test_master_data_generator.py`, `test_master_data_seed_db.py`, `test_v007_concurrency.py` |
| Dependencies | `python/requirements.txt`, `tests/python/requirements.txt` (points to the first) |
| Configuration and data | `config/generator.toml`, `config/.env.example`, `python/nordiccart/reference_data/*.json` |

19 Python files in total.

## Compatibility checks

| Check | Result |
|---|---|
| Every file compiled by Python 3.14 with `SyntaxWarning` as an error. This covers the new 3.14 warning for `return`/`break`/`continue` inside `finally` (PEP 765) and invalid escape sequences | 19 files, 0 problems |
| Standard library modules used: `argparse`, `copy`, `csv`, `dataclasses`, `datetime`, `decimal`, `hashlib`, `json`, `logging`, `os`, `pathlib`, `random`, `re`, `sys`, `threading`, `time`, `tomllib`, `zoneinfo` | All present and unchanged in 3.14 |
| Modules removed in 3.13 (PEP 594) and APIs deprecated or removed up to 3.14 (`datetime.utcnow`, `codecs.open`, `asyncio.get_event_loop`, `ast.Num`, `locale.getdefaultlocale`, `typing.ByteString`, ...) | None used |
| TOML reader | 3.14 uses the built-in `tomllib`; the `tomli` line in `requirements.txt` only applies below 3.11, so it is skipped |
| Deferred annotations (PEP 649/749) with `from __future__ import annotations` and dataclasses | Works; no warning in 3.14 |
| psycopg API used: `connect`, `Connection`, `transaction`, `cursor`, `executemany`, `rows.dict_row`, `errors`, `Error`, `OperationalError` | All stable in psycopg 3.3.9 on 3.14 |
| Deterministic randomness (`random.Random` text seeds) and `Decimal` maths | Identical output to Python 3.10 to 3.13 (fingerprints below) |
| Dependencies resolve on **Windows, Python 3.14, 64-bit** (checked with `pip download --platform win_amd64 --python-version 3.14`) | `psycopg_binary-3.3.9-cp314-cp314-win_amd64.whl`, `psycopg-3.3.9`, `tzdata-2026.6`, `pytest-9.2.0` and its pure-Python dependencies: all available |
| Minimum psycopg version for Windows 3.14 | psycopg-binary 3.2.10 is the oldest with a 3.14 Windows wheel. The `>=3.1` floor resolves to 3.3.9, so no change is needed |

## Test results on Python 3.14.6

Deprecation and pending-deprecation warnings were turned into errors for the unit and database tests.

| Test | Passed | Failed |
|---|---|---|
| psycopg connectivity: connect, numeric to `Decimal`, `timestamptz` (time-zone aware), `date`, UTF-8 text (`Jönköping`), transaction, advisory lock | all | 0 |
| `tests/python/test_master_data_generator.py` (unit tests, no database) | **48** | 0 |
| `tests/python/test_master_data_seed_db.py` (database tests, own databases) | **13** | 0 |
| `tests/python/test_v007_concurrency.py` (multi-session scenarios, own database) | **11** | 0 |
| Deliberate defect check: 10 safeguards broken one at a time, tests rerun | **10 caught** | 0 missed |

## Generator: 250 pilot records, reproducible

| Profile | Rows | Validation issues | Fingerprint (SHA-256) | Same as on 3.10 to 3.13 |
|---|---|---|---|---|
| pilot | **250**: currencies 4, countries 4, product_categories 10, products 20, product_prices 80, stock_locations 16, stores 12, warehouses 4, customers 100 | 0 | `c46181a25bd6fdc1...` | yes |
| full | 22,550: products 500, product_prices 2,000, customers 20,000 (others as pilot) | 0 | `68a225664b205294...` | yes |

The pilot rows are still exactly the first rows of the full dataset.

## Database-loading safeguards on Python 3.14

Command line on a fresh sandbox `nordiccart_oltp` (V001 to V007):

| Step | Result |
|---|---|
| Dry run | 250 rows generated, 0 validation issues, no database contacted |
| `--plan` | 250 rows would be inserted, nothing written |
| `--apply --yes` | 250 inserted, 250 verified, 250 `change_log` rows |
| `--apply --yes` again | nothing to insert, all 250 rows verified |
| `--apply` without `--yes` from a non-interactive shell | refused with exit code 4, nothing written |
| `sql/verification/11_verify_master_data.sql` | 38 PASS, 4 INFO, 0 FAIL |

The 13 database tests passed. They prove that:
- plan is read-only;
- a rerun inserts nothing;
- scaling up only adds rows;
- conflicts write nothing;
- later business changes count as drift, not conflicts;
- a taken email is refused;
- a failure half-way rolls everything back;
- dropped or changed rows are caught before COMMIT;
- two seeders at once do not duplicate;
- wrong or unmigrated databases are refused.

All 10 deliberately broken safeguards were caught on 3.14:
- the advisory lock;
- conflict detection;
- the email owner check;
- the single transaction;
- post-insert verification;
- the per-customer random stream;
- half-up rounding;
- the in-store sign-up rule;
- case-insensitive email uniqueness;
- keeping the password out of the log.

## Modifications made

| Type | Changes |
|---|---|
| Python files | none |
| `requirements.txt` (`python/` and `tests/python/`) | none |
| Migrations V001 to V007 | none |
| Configuration | none |
| New file | this report |

## Outstanding issues

1. **Exact version 3.14.7 not run here.** Tested on 3.14.6, the newest 3.14 build the sandbox could obtain. Confirm on your laptop with the steps below.
2. **Windows not run here.** The tests ran on Linux. The Windows packages for Python 3.14 were verified to exist. The code builds paths with `pathlib` and gets time zones on Windows from `tzdata`, which `requirements.txt` installs.
3. **Free-threaded Python (`python3.14t`) is not supported by psycopg-binary on Windows**: there is no `cp314t` Windows wheel for psycopg-binary 3.3.9. The standard installer build is the default and is fully supported. To check which build you have, run `python -c "import sys; print(sys._is_gil_enabled())"`. `True` means the standard build. If it prints `False`, use the standard `python.exe`, not `python3.14t.exe`.
4. **Future, no action now:** `from __future__ import annotations` (used in most modules) is planned to be deprecated only after Python 3.13 reaches end of life (about 2029, PEP 749). Python 3.14 gives no warning.

## Confirm on your laptop (Python 3.14.7, no database involved)

In the VS Code terminal (PowerShell), from the project folder:

```powershell
cd "<project folder>"
py -3.14 -m venv --clear .venv
.\.venv\Scripts\Activate.ps1
python --version
python -c "import sys; print(sys._is_gil_enabled())"
python -m pip install -r python\requirements.txt
python -m pytest tests\python\test_master_data_generator.py -q
python python\run_master_data_seed.py
```

Expected:
- `python --version` prints `Python 3.14.7`;
- the GIL check prints `True`;
- pip installs `psycopg-binary` (3.3.9 or newer) from a ready-made wheel, with no compiler messages;
- pytest prints `48 passed`. Its fingerprint test confirms your laptop generates exactly the same data;
- the dry run prints `generated 250 rows` and fingerprint `c46181a25bd6fdc1ad2c8fedab185c19802041c46b4aae703e66a0f58d768f64`.

Command variations:
- `py -3.14` needs the Python launcher. If `py` is not recognised and `python --version` already shows 3.14.7, use `python -m venv --clear .venv` instead.
- `--clear` rebuilds `.venv` for Python 3.14 if it was created with an older Python. Nothing else in the project is affected.
- If PowerShell refuses to activate, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` and repeat the activate line.
