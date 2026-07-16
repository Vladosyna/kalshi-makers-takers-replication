"""R3's firewall gate (docs/analysis_plan.md S4; Roadmap Phase 9).

R3 is a small, PROSPECTIVE, firewalled supplement: live order-book
depth/spread covariates BDW's own tape lacks, collected from 2026-07-04.
Its window mechanically overlaps R2's tail (through 2026-06-30) -- without
a firewall, an analyst could let R3's early signal color how an ambiguous
R2 verdict gets written up. Two independent checks enforce the rule:

1. require_r2_locked -- a runtime gate. R2's verdict (S2.2) must already be
   computed and locked to its own output artifact (kalshi_mt.r2.report's
   write_r2_report) BEFORE any R3 number is examined. R3 code that calls
   this first and lets it raise on a missing/unlocked artifact physically
   cannot proceed to look at R3 data before R2's verdict exists on disk.

2. check_no_r3_imports_outside_r3 -- a static gate. R2's own report
   generation must never import anything under r3/, in either direction:
   R3 is allowed to depend on R2's locked artifact (via require_r2_locked
   reading the JSON file R2 wrote), but R2's code must never import R3's
   code -- that would let an R3 module get pulled into R2's own process
   and, in principle, feed back into how R2's numbers get computed. Same
   regex technique as tests/test_scope.py's scope guard, kept here (not
   only in a test) so any future R3 pipeline code can import and call this
   check itself, not just CI. ONE deliberate, narrow exception: cli.py, the
   top-level command dispatcher, is excluded from this scan -- it is not
   "R2's report generation," it is the outer shell that must be able to
   expose a `kmt r3-check` command at all (which necessarily imports this
   very module to run the check), and its r3_check() function never feeds
   an r3 import into any of R1/R2's own computation functions. Every
   computational module (r1/, r2/, report/, fees/, control/, store/,
   api/, stepzero/) is still scanned without exception.

Only the gate lives here. No R3 data-fetch or analysis pipeline exists yet;
none is built in this module.
"""

from __future__ import annotations

import re
from pathlib import Path

from kalshi_mt.r2.report import load_r2_report


class R3FirewallError(RuntimeError):
    """Raised when R3 code tries to proceed without R2's own verdict already
    locked to disk -- distinct from load_r2_report's bare FileNotFoundError
    so callers can catch specifically "the firewall blocked this run" rather
    than any generic file-missing error."""


def require_r2_locked(path: str | Path | None = None) -> dict:
    """R3's runtime firewall gate: refuses to return unless R2's locked
    verdict artifact already exists on disk with a `locked_ts` field
    (kalshi_mt.r2.report.load_r2_report's own definition of "locked").
    Returns the loaded report dict unchanged on success -- callers may
    inspect its "verdict" key etc., though nothing in this repo needs to
    yet, since no R3 analysis pipeline exists.

    `path` defaults to PROJECT_ROOT / "reports" / "r2" / "verdict_lock.json",
    resolved via a LOCAL import (this codebase's own established
    convention, e.g. cli.py's command functions) rather than a module-level
    constant -- PROJECT_ROOT must be read fresh on every call, not frozen
    at first import, so that test monkeypatching of util.PROJECT_ROOT
    (test_stepzero.py's own established pattern) actually takes effect."""
    if path is None:
        from kalshi_mt.util import PROJECT_ROOT

        path = PROJECT_ROOT / "reports" / "r2" / "verdict_lock.json"
    try:
        return load_r2_report(path)
    except FileNotFoundError as exc:
        raise R3FirewallError(
            f"R3 firewall blocked this run: {exc} This is the R3 "
            "prospective-arm firewall (docs/analysis_plan.md S4): R2's "
            "verdict must be computed and locked to its own output artifact "
            "BEFORE any R3 number is examined, and R2's narrative is never "
            "revised in light of R3. This rule exists specifically because "
            "R3's window (from 2026-07-04) mechanically overlaps R2's tail "
            "(through 2026-06-30) -- without this gate, an analyst could let "
            "R3's early signal color how an ambiguous R2 verdict gets "
            "written up."
        ) from exc


_R3_IMPORT_PATTERNS = [
    re.compile(pattern, re.MULTILINE)
    for pattern in (
        r"^\s*(import|from)\s+kalshi_mt\.r3\b",
        r"^\s*from\s+kalshi_mt\s+import\s+.*\br3\b",
        r"^\s*from\s+\.\s+import\s+r3\b",
        r"^\s*from\s+\.r3\b",
        r"^\s*from\s+\.\.\s+import\s+r3\b",
        r"^\s*from\s+\.\.r3\b",
    )
]


_ALLOWED_R3_IMPORTERS = frozenset({"cli.py"})


def check_no_r3_imports_outside_r3(src_root: Path | None = None) -> list[str]:
    """Static, non-executing scan: no .py file anywhere under `src_root`
    may import kalshi_mt.r3 or any submodule of it, absolute or relative --
    EXCEPT files inside r3/ itself (naturally allowed to import their own
    siblings) and cli.py directly under `src_root` (the top-level command
    dispatcher; see the module docstring for why this one file is a
    deliberate, narrow exception, not an oversight). Mirrors
    tests/test_scope.py's regex technique. Covers every import spelling
    reachable in this repo's flat one-level-of-subpackages layout: `import
    kalshi_mt.r3` / `from kalshi_mt.r3 import ...`, `from kalshi_mt import
    r3` (the plain package-name spelling -- easy to miss since it doesn't
    literally contain "kalshi_mt.r3"), and the relative forms `from .
    import r3` / `from .r3 import ...` (from a file directly under
    kalshi_mt/) and `from .. import r3` / `from ..r3 import ...` (from a
    file inside a sibling subpackage such as r2/, fees/, report/). Returns
    the list of violating file paths as strings, empty if clean.

    `src_root` defaults to PROJECT_ROOT / "src" / "kalshi_mt", resolved via
    a local import for the same reason require_r2_locked's default is
    lazy -- PROJECT_ROOT must be read fresh on every call, not frozen at
    first import."""
    if src_root is None:
        from kalshi_mt.util import PROJECT_ROOT

        src_root = PROJECT_ROOT / "src" / "kalshi_mt"
    root = src_root
    violations = []
    for path in sorted(root.rglob("*.py")):
        relative_parts = path.relative_to(root).parts
        if relative_parts and relative_parts[0] == "r3":
            continue
        if relative_parts == (path.name,) and path.name in _ALLOWED_R3_IMPORTERS:
            continue
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in _R3_IMPORT_PATTERNS):
            violations.append(str(path))
    return violations
