"""Run the NordicCart opening inventory generator (VS Code: Run and Debug configurations, or the terminal).

Examples, from the repository folder:
    python python/run_opening_inventory.py            # dry run: generate, validate, CSV preview
    python python/run_opening_inventory.py --plan     # compare with the database, read-only
    python python/run_opening_inventory.py --apply    # insert listings and opening stock (asks to confirm)
Needs the master data loaded first (python/run_master_data_seed.py --apply).
See docs/phase3_opening_inventory_guide.md.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nordiccart.inventory.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
