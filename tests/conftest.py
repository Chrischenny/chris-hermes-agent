from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HERMES_ROOT = Path.home() / ".hermes" / "hermes-agent"
HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_ROOT", DEFAULT_HERMES_ROOT))

for path in (PROJECT_ROOT, HERMES_ROOT):
    resolved = str(path.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
