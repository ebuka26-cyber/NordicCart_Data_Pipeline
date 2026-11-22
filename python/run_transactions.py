"""Run the NordicCart pilot transaction simulation (VS Code: Run and Debug configurations, or the terminal).

Examples, from the repository folder:
    python python/run_transactions.py            # dry run: generate, validate, CSV preview
    python python/run_transactions.py --plan     # compare with the database, read-only
    python python/run_transactions.py --apply    # write orders, reservations, payments, fulfilment (asks to confirm)
Needs the master data and the opening inventory loaded first (run_master_data_seed.py and
run_opening_inventory.py with --apply). See docs/phase3_transactions_guide.md.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nordiccart.transactions.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
