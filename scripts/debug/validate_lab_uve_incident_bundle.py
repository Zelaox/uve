#!/usr/bin/env python3
"""UVE command_check entry — validates incident bundle structure and trust contract."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_server.diagnostics.lab_uve_bundle_validate import main

if __name__ == "__main__":
    raise SystemExit(main())
