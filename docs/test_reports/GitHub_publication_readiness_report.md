# GitHub publication readiness report

Date: 2026-11-22.

**Verdict: READY FOR PUBLICATION, awaiting the owner's explicit approval.** Nothing has been pushed, and no Git repository has been created. Git is not installed on this machine, so the commit file list was computed by applying the repository's `.gitignore` rules with a script.

**Scope of this stage:** documentation and publication hygiene only.
- **Not changed:** the database, the hourly pipeline, the ETL, FastAPI and the Power BI project. No code changed apart from documentation text.
- **Tests:** no tests were rerun.

## 1. Changes made in this stage

| File | Change |
|---|---|
| `README.md` (new) | Portfolio README: overview, key features, Mermaid architecture diagram, technology stack, the three dashboard screenshots, repository structure, setup, historical simulation, hourly scheduling, API usage, Power BI refresh, testing, security and documentation index |
| `.gitignore` | Added `.claude/` (local assistant state), `Claude outputs/` (empty local folder), and `/*.md` with `!/README.md`. The root copies of reports kept for review are duplicates of `docs/`, so only `README.md` is committed at the root |
| 7 documents in `docs/` | Machine-specific paths and the Windows user name replaced with neutral placeholders (section 3.3). Wording otherwise unchanged |
| `python/README.md` | Outdated rows corrected: the activity engine is no longer marked "planned"; the non-existent `extract/` row is replaced by the ETL, pipeline, API, exchange-rate and catch-up entries; `requirements.txt` lists the API packages |
| `docs/test_reports/GitHub_publication_readiness_report.md`, `docs/test_reports/Project_completion_report.md` (new) | This report and the completion report |

## 2. Repository structure (what a commit would contain)

**284 files, 3.65 MB.** Every file is under 1 MB; the largest is the Power BI base theme (145 KB).

| Folder | Files | Contents |
|---|---:|---|
| (root) | 2 | `README.md`, `.gitignore` |
| `.vscode/` | 1 | `launch.json` (run configurations, `${workspaceFolder}` paths only) |
| `config/` | 3 | `.env.example` (placeholder values), `generator.toml`, `README.md` |
| `database/` | 17 | DBML design (3), setup script, migrations V001–V009, rollbacks R008–R009, V008 privilege re-apply, `README.md` |
| `docs/` | 55 | 13 guides and design documents, 42 stage and test reports |
| `powerbi/` | 75 | PBIP project (TMDL model, PBIR report: 3 pages, 46 visuals, theme), `build_pbip.py`, `MEASURES.md`, `README.md`, `.gitignore`, 3 screenshots |
| `python/` | 83 | The `nordiccart` package (59 modules plus reference-data JSON), entry points, the pipeline launcher and scripts, `requirements.txt`, `README.md` |
| `sql/` | 19 | 18 verification scripts, `README.md` |
| `tests/` | 29 | 23 pytest modules plus `conftest.py` and `requirements.txt`, 5 SQL test suites, `README.md` |

The full tree with descriptions is in `README.md`, section "Repository structure".

**Excluded from the commit, and why:**

| Path | Size | Reason |
|---|---:|---|
| `config/.env`, `config/.env.scheduler`, `config/.env.extractor` | – | Credentials (`.env`, `*.env`, `.env.*`; only `.env.example` is re-included). Their contents were not read |
| `database/backups/` | 17 dumps, 626 MB | Complete database backups |
| `logs/` | 839 files, 184 MB | Run logs |
| `output/` | 100 files, 156 MB | Generated data: master-data previews, raw, cleaned and analytics ETL layers. All of it can be regenerated |
| `.venv/`, `.pytest_cache/`, `__pycache__/` | 54 MB | Environment and caches |
| `.claude/`, `Claude outputs/` | – | Local tool state |
| `*.md` at the root except `README.md` | 9 files | Review copies of `docs/` reports |
| `powerbi/**/.pbi/localSettings.json`, `cache.abf` | – | Power BI Desktop local settings and data cache (`powerbi/.gitignore`) |

## 3. Security check results

The scan covered every file the `.gitignore` rules include: 282 files before the two new reports. The two reports contain no paths or credentials. Secret-looking values were never printed; they were assessed with the value masked.

### 3.1 Files

| Check | Result |
|---|---|
| Credential files (`.env*` other than the template, `pgpass`) | **None included** |
| Database dumps or backups (`*.dump`, `*.backup`) | **None included** |
| Logs (`*.log`, `logs/`) | **None included** |
| Power BI data cache (`cache.abf`) | **None included** |
| Files over 1 MB | **None** |

### 3.2 Content

| Pattern | Result |
|---|---|
| Private keys, API tokens (`ghp_`, `sk-`, `AKIA`), connection URIs with a password | **0** |
| `PGPASSWORD=` or `password = "…"` with a literal value | 6 hits, **all harmless** (see below) |
| E-mail addresses on personal domains | **0** (generated customers use `example.com`/`.net`/`.org`) |
| Windows user name | **0** after the redaction (was 15 occurrences in 7 documents) |
| Windows user-profile paths | **0** after the redaction |
| Generic `APPDATA` instructions for the PostgreSQL password file | 5 occurrences in 2 V008 reports. These are generic instructions that use an environment variable and contain no personal data, so they are kept |

The six password hits:
- `config/.env.example`: the placeholder `change_me`.
- `docs/phase2_valentina_studio_execution_guide.md`: a `Read-Host` prompt, not a value.
- `docs/phase3_replenishment_guide.md`: the disposable sandbox cluster's literal `unused` (local trust authentication).
- `tests/python/test_master_data_generator.py` (2) and `tests/python/test_opening_inventory.py` (1): fake values used by tests that prove passwords are never logged.

### 3.3 Redaction

| Document | Replaced |
|---|---|
| `docs/phase2_valentina_studio_execution_guide.md` | 3 project paths → `<project folder>` |
| `docs/phase3_master_data_generator_guide.md` | 1 project path |
| `docs/test_reports/2026-11-17_python_3_14_compatibility_report.md` | 1 project path |
| `docs/test_reports/2026-11-18_pre_replenishment_backup_and_readiness_report.md` | 1 project path |
| `docs/test_reports/2026-11-19_s4_v009_real_deployment_report.md` | 5 project paths |
| `docs/test_reports/S4_V009_VERIFICATION_CHECKPOINT.md` | 1 assistant scratchpad path → `<session scratchpad folder>` |
| `docs/test_reports/S10_activation_status.md` | Task account name → "the current Windows user". The root review copy was updated and is byte-identical |

### 3.4 Other items reviewed

| Item | Assessment |
|---|---|
| PostgreSQL cluster system identifier `7663257437785384692` | **Kept.** It appears in `activity/run.py`, `run_pipeline_hourly.cmd`, `setup_pipeline_credentials.ps1`, `s9_catch_up.py` and several reports. It is a random number that identifies the original local server for the engine's write guard. It is not a credential, cannot be used to connect, and the server listens on localhost only. Removing it would change the running hourly pipeline, which is out of scope |
| Power BI project | The data-folder parameter is the neutral placeholder `C:\NordicCart_Data_Pipeline\output\etl\analytics`. The three screenshots show only the report canvas: no account, file path or other window |
| `.vscode/launch.json` | Only `${workspaceFolder}`-relative paths |
| FastAPI `/health` | Returns the absolute analytics folder at run time. This is not in the repository; the API listens on localhost only by default |
| Git history | None exists yet, so no secret can be in history. The first commit should be made from this state |

## 4. Reproducibility check

The 282 publishable files were copied to a clean folder outside the project: no `output/`, `logs/`, backups, `.venv` or credentials. These checks were run there, with no database and nothing written to the project:

| Check | Result |
|---|---|
| Inputs present: `config/.env.example`, `config/generator.toml`, `00_create_database.sql`, migrations V001–V009, `requirements.txt`, `NordicCart.pbip` | **PASS** |
| No credential file in the copy | **PASS** |
| Every `nordiccart` module imports (59 modules) | **PASS** |
| `pytest --collect-only` collects every test module | **PASS**: 554 tests collected (collection only, not run) |
| `python powerbi/build_pbip.py` regenerates the committed Power BI project | **PASS**: byte-identical, every file |
| README relative links resolve | **PASS** after this report and the completion report were added. Re-run on the final state: section 6 |
| README code paths | All exist, except the files the README names on purpose as git-ignored and created at setup (`config/.env`, `config/.env.scheduler`, `config/.env.extractor`, `database/backups/`) and one prefix (`sql/verification/01`–`18`) |

**Reproduction path documented in the README:** Python environment → database and migrations → role passwords and `config/.env` → baseline loaders (pilot, then full profile) → historical simulation → hourly scheduling → API → Power BI. The simulation is seeded and deterministic: the real deployment reproduced the sandbox rehearsal's fingerprints for all 698 windows.

**Known limitations, stated in the README:**
1. **Pinned write guard.** The engine's production authorization is pinned to the original server's cluster identifier, in 4 places. A new server uses sandbox mode, or replaces the identifier. This is by design, so the engine can never write to an unintended server.
2. **Not rebuilt end to end.** The full build order was executed and verified stage by stage during the project (each step's report is in `docs/test_reports/`). It was not repeated end to end on a fresh server for publication, which would mean another full database build: about an hour of simulation, and out of scope.
3. **Windows-specific automation.** Scheduling uses Windows Task Scheduler and PowerShell scripts. The Python code is cross-platform.
4. **Manual Power BI refresh.** Power BI Desktop refresh is manual. A scheduled cloud refresh would need the Power BI Service and a gateway.

## 5. Decisions and steps that need you

1. **Approve publication.** Nothing is pushed until you say so.
2. **Choose a licence.** No `LICENSE` file is included. Common choices for a portfolio are MIT, or none (all rights reserved).
3. **Two old files in `docs/`, cosmetic.**
   - `docs/Database_Schema.md.txt` is empty (0 bytes).
   - `docs/Business_Rules.md.txt` has a double extension.
   - Neither is referenced by the README. They are left as they are; delete or rename them if you prefer.
4. **Publish.** Install Git, create an empty repository on GitHub (public or private, without a generated README or .gitignore), then from the project folder:
   ```powershell
   git init -b main
   git add .
   git status            # check: no config/.env*, database/backups, logs, output; 284 files
   git commit -m "NordicCart data pipeline: PostgreSQL OLTP, simulation, hourly ETL, FastAPI, Power BI"
   git remote add origin https://github.com/<your-account>/<repository>.git
   git push -u origin main
   ```
   I can run these steps once you approve and Git is installed. After the push, check on GitHub that the Mermaid diagram and the screenshots render.

## 6. Final check after writing this report

| Check | Result |
|---|---|
| Publishable files | 284 (282 plus the two new reports) |
| Security scan | 0 credential files, 0 backups or logs, 0 user paths, 0 user names. Only the 6 harmless password hits and the 5 generic `APPDATA` instructions of section 3.2 remain |
| Clean-copy check (284 files) | 20 of 21 checks PASS: all 28 README links resolve; 59 modules import; 554 tests collected; Power BI regenerates byte-identically. The one non-pass is expected: the README names the git-ignored files created at setup (`config/.env*`, `database/backups/`) |
| Root review copies | Byte-identical to `docs/`, and git-ignored |

## 7. Git verification (2026-11-22)

**Status: repository initialized and files staged. NOT committed, NOT pushed.** 0 commits, no remote.

| Check | Result |
|---|---|
| Git access | Not on the terminal PATH. The copy bundled with GitHub Desktop 3.6.6 (`git version 2.53.0.windows.4`, under `%LOCALAPPDATA%\GitHubDesktop\app-3.6.6\resources\app\git\cmd\git.exe`) was used by its full path |
| Repository | `git init -b main` in the existing `NordicCart_Data_Pipeline` folder (no new folder). Branch `main` |
| Staged (`git add .`) | **284 files, 3.49 MB**: exactly the 284 files predicted in section 2. The comparison found 0 differences in either direction |
| Exclusions, confirmed with `git check-ignore -v` | `config/.env`, `config/.env.scheduler`, `config/.env.extractor`; `database/backups/`; `logs/`; `output/`; `.venv/`; `.pytest_cache/`; `__pycache__/`; `.claude/`; `Claude outputs/`; the 9 root review copies. `config/.env.example` is staged as the only `.env` file |
| Staged names matching sensitive patterns (`.env`, pgpass, `.dump`, `.backup`, `.log`, `.abf`, `localSettings`, `output/`, `logs/`, backups, `.venv`, `__pycache__`) | **0** |
| `git grep --cached` over the staged content | User name or user-profile paths: **0**. Personal e-mail domains: **0**. Private keys, tokens, connection URIs with a password: **0**. `PGPASSWORD=` lines: 7 files, all placeholders, prompts, test fixtures or this report's own description (section 3.2; plus `"..."` in `docs/phase3_master_data_generator_guide.md`) |
| Line endings | GitHub Desktop sets `core.autocrlf=true`. Git stores text with LF line endings, and the warnings only mean the working copy may be checked out with CRLF later. File contents are unaffected |

**Security concern before the first commit: commit identity.** Git's `user.name` and `user.email` are configured, and the e-mail is **not** a GitHub "noreply" address. Every commit records the author e-mail, and it becomes public in a public repository. The values were not displayed. Recommended, for this repository only:
```powershell
git config user.email "<id>+<username>@users.noreply.github.com"   # shown on GitHub: Settings → Emails
```
Alternatively, keep your address if you are happy for it to be public. GitHub Desktop shows and edits the same setting (File → Options → Git).

**Final publication-readiness status: READY.** No credential, backup, log, generated-data or local file is staged.
1. **Identity:** choose the commit identity (above).
2. **Approve:** once you approve, the commit and the push to a new GitHub repository can be done from GitHub Desktop (File → Add local repository → this folder → Commit to main → Publish repository) or from the terminal.
