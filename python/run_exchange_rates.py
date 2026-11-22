"""Plan or load the approved 96-row E1 window. Default mode is read-only."""
import argparse
from datetime import date
import logging
import sys

import psycopg

from nordiccart.exchange_rates import E1_END, reconcile
from nordiccart.logging_setup import setup_logging
from nordiccart.master_data.load import connect
from nordiccart.settings import describe_connection_target, load_env_file, load_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    if settings.simulation_start != date(2024, 11, 1):
        raise ValueError("E1 must begin on 2024-11-01")
    setup_logging(settings.log_dir, "exchange_rates_e1")
    log = logging.getLogger("nordiccart.exchange_rates")
    load_env_file()
    log.info("Target %s", describe_connection_target())
    with connect() as conn:
        database, role = conn.execute("SELECT current_database(),session_user").fetchone()
        if role != "nordiccart_loader":
            raise ValueError("Use the nordiccart_loader login")
        pending = reconcile(conn, settings, settings.simulation_start, E1_END)
        log.info("E1: %d rows pending on %s as %s", len(pending), database, role)
        if args.apply and pending:
            if input("Type the target database name to commit E1: ") != database:
                raise ValueError("Confirmation declined")
            reconcile(conn, settings, settings.simulation_start, E1_END, apply=True, confirmed=pending)
            log.info("Committed %d E1 rows", len(pending))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, EOFError, psycopg.Error) as error:
        logging.getLogger("nordiccart.exchange_rates").error("%s", error)
        sys.exit(1)
