"""The scope guard.

Fails if execution-layer code appears anywhere under src/: order-placement
endpoints/functions, or RSA private-key material (Kalshi's authenticated
trading API signs requests with an RSA key pair, not eth-style signing --
the forbidden list is repo-specific, not copied from the Polymarket lab's
web3/eth_account list). Keeps this repository exactly what it claims to be:
a read-only replication instrument, no execution surface.

FORBIDDEN_IMPORTS is intentionally empty -- no canonical "Kalshi trading
SDK" package name is established the way web3/py_clob_client is for
Polymarket. The string checks below carry the real weight; populate this
list if/when a specific trading SDK becomes relevant.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

FORBIDDEN_IMPORTS: tuple[str, ...] = ()
FORBIDDEN_STRINGS = ("private_key", "/portfolio/orders", "create_order", "cancel_order", "place_order")

IMPORT_PATTERNS = [
    re.compile(rf"^\s*(import|from)\s+{re.escape(mod)}\b", re.MULTILINE)
    for mod in FORBIDDEN_IMPORTS
]


def _iter_source_files():
    files = sorted(SRC.rglob("*.py"))
    assert files, f"no Python sources found under {SRC}"
    return files


def test_no_forbidden_imports():
    violations = []
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8")
        for pattern in IMPORT_PATTERNS:
            if pattern.search(text):
                violations.append(f"{path}: matches {pattern.pattern!r}")
    assert not violations, "execution-layer imports found:\n" + "\n".join(violations)


def test_no_forbidden_strings():
    violations = []
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8").lower()
        for needle in FORBIDDEN_STRINGS:
            if needle.lower() in text:
                violations.append(f"{path}: contains {needle!r}")
    assert not violations, "execution-layer strings found:\n" + "\n".join(violations)
