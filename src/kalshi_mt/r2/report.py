"""R2 verdict-lock artifact (docs/analysis_plan.md S2; Roadmap Phases 9-10):
assembles the same result dict `kmt r2` prints to stdout, plus persists it
to disk WITH A LOCKED TIMESTAMP -- the concrete artifact r3/firewall.py's
require_r2_locked gates on, and report/final.py reads from. R2's verdict is
computed and locked here BEFORE any R3 number is examined (spec S4's
firewall rule); nothing under r3/ may import this module's writer -- see
r3/firewall.py's own static import-direction check.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kalshi_mt.r2.decomposition import DecompositionResult
from kalshi_mt.r2.horizon import HorizonRobustnessResult
from kalshi_mt.r2.verdicts import DeltaBarEstimate
from kalshi_mt.util import now_utc_iso


def build_r2_report(
    *, r2_filters: dict[str, Any], psi_bar_r1: float | None,
    r1_panel_n: int, r2_panel_n: int, pooled_panel_n: int,
    categories_fit: list[str],
    delta_bar: dict[str, DeltaBarEstimate | None],
    verdict: dict[str, str | None],
    decomposition: dict[str, DecompositionResult],
    horizon: HorizonRobustnessResult,
) -> dict[str, Any]:
    """Pure assembly -- no I/O, no clock call. Same shape `kmt r2` already
    prints to stdout; factored out here so write_r2_report persists
    exactly what the operator saw, not a re-derived summary."""
    return {
        "r2_filters": r2_filters,
        "psi_bar_r1": psi_bar_r1,
        "r1_panel_n": r1_panel_n,
        "r2_panel_n": r2_panel_n,
        "pooled_panel_n": pooled_panel_n,
        "categories_fit": categories_fit,
        "delta_bar": {k: (None if v is None else asdict(v)) for k, v in delta_bar.items()},
        "verdict": verdict,
        "decomposition": {k: asdict(v) for k, v in decomposition.items()},
        "horizon_robustness": asdict(horizon),
    }


def write_r2_report(report: dict[str, Any], path: str | Path) -> Path:
    """Stamps `locked_ts` (the one clock call in this module) and persists.
    This timestamp is what makes the artifact "locked": r3/firewall.py
    checks for its presence, not merely the file's existence, so a
    hand-crafted file missing this field doesn't accidentally satisfy the
    gate."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"locked_ts": now_utc_iso(), **report}
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def load_r2_report(path: str | Path) -> dict[str, Any]:
    """Raises FileNotFoundError (not a bare KeyError/generic exception)
    when the locked artifact is missing or malformed -- Phase 9's firewall
    and Phase 10's report assembly both surface this same message rather
    than re-deriving their own wording, matching r1/reconcile.py's
    load_frozen_2024_mix convention."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing -- R3 (Phase 9) and the final report (Phase 10) both "
            "require R2's own locked verdict artifact to already exist (kmt r2's own "
            "write_r2_report). It is never recomputed here."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "locked_ts" not in payload:
        raise FileNotFoundError(
            f"{path} exists but has no 'locked_ts' field -- not a genuine locked R2 "
            "artifact (kmt r2's own write_r2_report always stamps one); refusing to "
            "treat it as satisfying the firewall."
        )
    return payload
