"""Run the NordicCart replenishment pilot (VS Code: Run and Debug configurations, or the terminal).

Examples, from the repository folder:
    python python/run_replenishment.py            # dry run: generate, validate, CSV preview
    python python/run_replenishment.py --plan     # compare with the database, read-only
    python python/run_replenishment.py --apply    # write reorder points, receipts, transfers (asks to confirm)
Needs the master data, the opening inventory and the complete pilot transactions loaded first
(run_master_data_seed.py, run_opening_inventory.py and run_transactions.py with --apply).
See docs/phase3_replenishment_guide.md.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nordiccart.replenishment.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
