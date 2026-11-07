"""Gateway child for subprocess tests: ``session_stdio`` with ``type: scripted``.

Run as ``python tests/scripted_child.py --config PATH …`` (same arguments as
``python -m agent_augury.gateway.session_stdio``).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.scripted import install

if __name__ == "__main__":
    install(setattr)
    from agent_augury.gateway.session_stdio import main

    raise SystemExit(main())
