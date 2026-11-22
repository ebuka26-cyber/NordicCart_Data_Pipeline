"""Allows `python -m nordiccart.transactions` from the python/ folder."""

import sys

from .cli import main

sys.exit(main())
