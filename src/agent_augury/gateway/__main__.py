"""Package entry: ``python -m agent_augury.gateway`` runs the M2 hello demo."""

from __future__ import annotations

from .hello_demo import main

if __name__ == "__main__":
    raise SystemExit(main())
