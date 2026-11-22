# Test report: Phase 3 master data generator, disposable sandbox

Date: 2026-11-17. Environment: PostgreSQL 16.15 in a throwaway instance in Claude's workspace, deleted afterwards. Python 3.13.18 with psycopg 3.3.9; the generator was also run on Python 3.10.20, 3.11.18 and 3.12.3. The real NordicCart database was not touched.

## Run

1. Fresh `nordiccart_oltp` from `database/setup/00_create_database.sql`, migrations V001 to V007: history `001,...,007`
2. Unit tests (no database)
3. Seeder on the pilot profile: dry run, plan, apply, rerun, third run
4. `sql/verification/11_verify_master_data.sql`, default `search_path` and `search_path=pg_catalog`
5. All Phase 2 verification scripts and SQL behaviour tests, with the master data present
6. Database tests (their own databases, created from V001 to V007 and dropped)
7. Full profile (500 products, 20,000 customers): dry run; plan against the pilot database; apply to a copy; script 11; rerun
8. Mutation checks
9. Reproducibility across Python versions

## Results

| Check | Result |
|---|---|
| Unit tests `test_master_data_generator.py` | **48 passed**, 0 failed |
| Dry run, pilot | 250 rows generated, validation 0 issues, fingerprint `c46181a25bd6fdc1...`, 0.4 s |
| `--plan` on the empty database | 250 rows would be inserted, 0 conflicts, nothing written |
| `--apply` | **250 rows inserted, 250 verified, 250 change_log rows**, 0.2 s |
| `--apply` again | `nothing to insert: the database already holds all 250 rows (verified)`; exit code 0 |
| Script 11 (both `search_path` settings) | **42 rows: 38 PASS, 4 INFO, 0 FAIL** |
| Verification 01 to 10 after seeding | 477 PASS, 0 FAIL, 2 WARN (the expected V007 triggers in script 07), 72 INFO; same as the V007 report |
| SQL tests after seeding | foundation 44/44, V006 119/119, V007 105/105; master rows unchanged afterwards (100 customers, 20 products, 80 prices, 0 orders) |
| Database tests `test_master_data_seed_db.py` | **13 passed**, 0 failed |
| Full profile, dry run | 22,550 rows, validation 0 issues, 4.6 s |
| Full profile, plan on the pilot database | inserts 480 products, 1,920 prices, 19,900 customers; 0 conflicts |
| Full profile, apply to a copy | 22,300 rows inserted, 22,550 verified, 9.7 s; script 11 with 500/20000: 38 PASS, 0 FAIL; rerun inserts nothing |

## Database tests in detail

| Test | Proves |
|---|---|
| plan is read-only | 250 to insert, every table still empty afterwards |
| apply then rerun | 250 rows, then 0; customer ids unchanged |
| script 11 after the pilot | 38 PASS, 4 INFO, 0 FAIL from Python too |
| scaling up only adds rows | pilot then 30 products / 150 customers: inserts 10 + 40 + 50, pilot ids unchanged; the pilot run afterwards inserts nothing and reports 50 extra customers |
| conflict writes nothing | a customer edited by hand stops a larger run; none of the 20 new customers is inserted |
| lifecycle changes are drift | a closed price and a customer who moved and was deactivated: 0 conflicts, reported as drift |
| taken email is a conflict | the email of a new customer already used, in upper case, by another customer |
| failure half-way rolls back | a CHECK added on customers fails the last table; all nine tables stay empty |
| dropped or changed rows caught | a trigger skips one customer and alters another; verification fails before COMMIT, everything rolled back |
| two seeders at once | advisory lock: one inserts 250, the other 0; 100 customers |
| database without V007 / wrong database | refused before any read of the master tables |
| command line | `--plan`, `--apply --yes` twice: exit codes 0, logs show 250 then nothing to insert |

## Mutation checks (do the tests catch defects?)

One defect introduced at a time in a copy of the code, then all unit and database tests run:

| Defect | Caught by |
|---|---|
| advisory lock removed | two seeders at once |
| changed rows counted as present (no conflict detection) | conflict writes nothing |
| email owner check removed | taken email is a conflict |
| inserts outside one transaction | 9 database tests |
| post-insert verification skipped | dropped or changed rows caught (added after the first round missed this defect) |
| customer random stream depends on the profile size | fingerprint, pilot-is-prefix-of-full, scaling up, and 2 more |
| money rounded half-to-even instead of half-up | money rounding test |
| in-store sign-up rule removed from validation | validator test |
| case-sensitive email uniqueness in validation | validator test |
| password shown in the connection log line | password never reaches the log |

10 of 10 caught.

## Reproducibility

The pilot fingerprint `c46181a25bd6fdc1...` and the full fingerprint `68a225664b205294...` are identical on Python 3.10.20, 3.11.18, 3.12.3 and 3.13.18. The generator avoids the usual sources of drift between machines:
- every entity has its own text-seeded random stream, seeded through SHA-512 and independent of PYTHONHASHSEED;
- only stable `random` methods are used;
- Decimal arithmetic replaces float `exp`/`log`/`pow`.

The pilot's 100 customers and 20 products are identical to the first 100 and 20 of the full dataset.

## Not tested here

- Windows and Valentina Studio: the scripts ran on Linux with psql and psycopg. The code uses `tzdata` for time zones on Windows and paths built with `pathlib`.
- PostgreSQL 13 to 15: the sandbox ran 16. The seeder uses nothing newer than 13.
