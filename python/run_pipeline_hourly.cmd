@echo off
REM NordicCart hourly pipeline: launched by Windows Task Scheduler ("NordicCart Hourly Pipeline", registered DISABLED).
REM Activation needs explicit owner authorization and two credentials that do not exist yet:
REM   config\.env.scheduler  PGHOST/PGPORT/PGDATABASE/PGUSER=nordiccart_scheduler (+ PGPASSWORD or pgpass)
REM   config\.env.extractor  the same with PGUSER=nordiccart_extractor
REM Without the authorization line below the engine's write guard refuses the real database (exit 3, nothing written).
set NORDICCART_S9_REAL_AUTHORIZED=7663257437785384692/nordiccart_oltp
cd /d "%~dp0.."
".venv\Scripts\python.exe" -B python\run_pipeline.py --env-file config\.env.scheduler --etl-env-file config\.env.extractor
exit /b %ERRORLEVEL%
