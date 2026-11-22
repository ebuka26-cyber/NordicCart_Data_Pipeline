"""Allows `python -m nordiccart.inventory` from the python/ folder."""

import sys

from .cli import main

sys.exit(main())
