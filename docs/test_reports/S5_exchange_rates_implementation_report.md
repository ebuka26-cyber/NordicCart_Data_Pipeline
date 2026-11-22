# S5 daily exchange rates · implementation report

November 19, 2026. **READY for controlled real-database deployment review.** The E1 backfill passed on a disposable PostgreSQL 18.4 copy of the verified S4 state. All S5 database connections during implementation targeted the sandbox at 127.0.0.1:55432. The real database remains at its verified S4 state as last recorded. Live readiness and backup freshness must be checked at deployment.

## Generation and loading

The design in sections 8.3 and 9.1 calls for a EUR-base synthetic series. Sweden, Norway and Denmark each get a quote per calendar day. Finland uses EUR directly. The full approved calendar produces 2,190 rates over 730 days. E1 covers November 1 through December 2, 2024 inclusive, with the remaining 2,094 rows reserved for later daily windows.

The generator uses the existing seed, per-entity stream helper, Decimal normal sampler and configured price reference rates. Each currency/date draws an independent deterministic innovation, so interruption or window boundaries do not consume another currency’s stream. X1 validates the stored prefix against the seed and uses the persisted prior-day values to continue. The daily update retains 97% of the previous log deviation from the configured centre, adds the seeded shock and rounds to four decimal places. The source is the V003 default `synthetic_ecb`.

Implementation choices for review: daily shock scales are 0.4% for SEK, 0.5% for NOK and 0.04% for DKK. SEK and NOK have a ±15% sanity band. DKK uses the approved ±0.3% peg. Inward rounding keeps four-decimal values inside the bands. The synthetic publication calendar carries the preceding rate across weekends, New Year’s Day, Good Friday, Easter Monday, May 1 and December 25–26. Easter dates are specified for the approved simulation interval.

The CLI defaults to a read-only plan. Apply mode checks the loader login, displays the plan and requires the operator to type the connected database name. Inside one transaction it takes an X1 advisory lock, repeats validation, compares the pending rows with the confirmed plan and inserts only missing rows. Existing matching rows are reused. Conflicting values, unexpected series metadata, gaps before an already loaded date or a changed confirmation stop the write. E1 is atomic; daily continuation uses the same loader with a single-day window. Database identity, uniqueness, change capture and insert-only enforcement remain V003/V008 responsibilities.

## Sandbox results

Final command: `python -B -m pytest tests/python/test_exchange_rates.py -p no:cacheprovider -v --tb=short`, with `NORDICCART_S5_SANDBOX=1`. **9 passed, 0 failed, 0 skipped**, exit 0, Python 3.14.7. Tests created disposable clones of `nordiccart_oltp_v9r_rehearsal`, reapplied the existing V008 database privileges to each clone and used the loader for writes and the analyst for the full SQL verification suite. The clones were removed and the sandbox was stopped.

| Check | Expected | Actual |
|---|---:|---:|
| Full calendar generated in memory | 2,190 rows | 2,190 |
| E1 initial plan, rows pending | 96 | 96 |
| Rows stored after read-only plan | 0 | 0 |
| Rates after committed E1 | 96 | 96 |
| Change-log count after E1 | 1,714 (1,618 + 96) | 1,714 |
| Repeat apply and repeat plan, rows pending | 0 each | 0 each |
| Orders / movements / units after E1 and reruns | 20 / 326 / 1,467 | 20 / 326 / 1,467 |
| Script 16 after E1 | 4 PASS | 4 PASS |
| Scripts 01–18 after E1 | 0 FAIL | 0 FAIL |
| December 3 daily continuation | +3 rates, +3 change-log entries | 99 rates, 1,717 change-log entries |
| Stale confirmed plan or conflicting stored value | Refusal before insert | Both refused; only the two test setup rows remained |

Script 16 also detected all 96 missing rows on the empty sandbox series. It checks the calendar grid, pair/source/precision and bands, closed-day carry and coverage of every non-EUR fulfilled order on its market-local fulfilment date. Script 11 check 207 previously required zero rates; it retains its PASS on the empty pilot and reports the populated series as INFO with coverage delegated to script 16. The final sandbox run found no outstanding errors.

CLI evidence is in `logs/exchange_rates_e1_20261119_183347.log`, `logs/exchange_rates_e1_20261119_183348.log` and `logs/exchange_rates_e1_20261119_183349.log`.

## Implemented and modified files

| Source | Change |
|---|---|
| `python/nordiccart/exchange_rates.py` | New generator and X1 transaction/validation logic |
| `python/run_exchange_rates.py` | New E1 plan and confirmed apply CLI using existing settings, connection and logging |
| `sql/verification/16_verify_exchange_rates.sql` | New read-only series and fulfilment checks |
| `sql/verification/11_verify_master_data.sql` | Narrow S5 compatibility update to check 207 |
| `tests/python/test_exchange_rates.py` | New deterministic-series, calendar, CLI, conflict, rerun and integration checks |

Documentation: this report, `docs/test_reports/S5_EXCHANGE_RATES_CHECKPOINT.md`, and the S5 entry in `docs/phase2_database_implementation_status.md`. Project-root copies are provided for all three. No migration or generator configuration was edited.

## Remaining work

Review the model choices and sandbox evidence. For a separately authorized real deployment, verify the current S4 state and recovery point, then run `python/run_exchange_rates.py --plan` with the project interpreter and loader identity. The expected initial real plan is 96 pending rows. No live E1 run was performed in S5 implementation.
