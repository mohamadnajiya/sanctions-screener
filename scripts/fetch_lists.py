#!/usr/bin/env python3
"""Download the official sanctions lists and rewrite data/lists/*.jsonl + data/version.yaml.

    uv run python scripts/fetch_lists.py                 # all lists
    uv run python scripts/fetch_lists.py --only un uae   # a subset
    uv run screen refresh                                 # same thing via the CLI (used by the weekly cron)

This is the only code path with network access. Commit the resulting snapshot (and the version.yaml) with a
`data:` commit so the Docker image and the Apify Actor build from a known list state.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from screen.refresh import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
