"""Tests for R3's firewall gate (docs/analysis_plan.md S4; Roadmap Phase 9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kalshi_mt.r2.decomposition import DecompositionResult
from kalshi_mt.r2.horizon import HorizonBucketResult, HorizonRobustnessResult
from kalshi_mt.r2.report import build_r2_report, write_r2_report
from kalshi_mt.r2.verdicts import DeltaBarEstimate
from kalshi_mt.r3.firewall import (
    R3FirewallError,
    check_no_r3_imports_outside_r3,
    require_r2_locked,
)


def _sample_report_kwargs():
    return dict(
        r2_filters={"total": 10, "passed": 10, "failed": 0, "reason_counts": {}},
        psi_bar_r1=0.041,
        r1_panel_n=100, r2_panel_n=50, pooled_panel_n=150,
        categories_fit=["Politics", "Weather"],
        delta_bar={
            "fee": DeltaBarEstimate(delta_bar=-1.0, ci_lo=-1.5, ci_hi=-0.5),
            "publication": None,
        },
        verdict={"fee": "attenuated", "publication": None},
        decomposition={
            "fee": DecompositionResult(within=-1.0, between=0.1, aggregate=-0.9, per_category={}),
            "publication": DecompositionResult(within=0.0, between=0.0, aggregate=0.0, per_category={}),
        },
        horizon=HorizonRobustnessResult(
            by_bucket=[HorizonBucketResult(lookback_day=0, n=50, delta_bar_fee=-1.0, delta_bar_pub=None, n_categories_fit=2)],
            close_only=HorizonBucketResult(lookback_day=0, n=50, delta_bar_fee=-1.0, delta_bar_pub=None, n_categories_fit=2),
        ),
    )


# ---------------------------------------------------------------------------
# require_r2_locked
# ---------------------------------------------------------------------------

def test_require_r2_locked_missing_artifact_raises_firewall_error(tmp_path):
    with pytest.raises(R3FirewallError, match="R3 firewall blocked this run"):
        require_r2_locked(tmp_path / "nope.json")


def test_require_r2_locked_missing_locked_ts_raises_firewall_error(tmp_path):
    path = tmp_path / "fake.json"
    path.write_text('{"psi_bar_r1": 0.5}', encoding="utf-8")
    with pytest.raises(R3FirewallError, match="locked_ts"):
        require_r2_locked(path)


def test_require_r2_locked_is_not_a_bare_file_not_found_error(tmp_path):
    with pytest.raises(R3FirewallError) as excinfo:
        require_r2_locked(tmp_path / "nope.json")
    assert not isinstance(excinfo.value, FileNotFoundError)
    assert isinstance(excinfo.value.__cause__, FileNotFoundError)


def test_require_r2_locked_succeeds_on_genuine_locked_artifact(tmp_path):
    report = build_r2_report(**_sample_report_kwargs())
    path = write_r2_report(report, tmp_path / "r2" / "verdict_lock.json")

    loaded = require_r2_locked(path)

    assert "locked_ts" in loaded
    assert loaded["verdict"]["fee"] == "attenuated"
    assert loaded["psi_bar_r1"] == 0.041


# ---------------------------------------------------------------------------
# check_no_r3_imports_outside_r3
# ---------------------------------------------------------------------------

def test_check_no_r3_imports_outside_r3_clean_on_real_tree():
    violations = check_no_r3_imports_outside_r3()
    assert violations == []


def test_check_no_r3_imports_outside_r3_allows_cli_py_but_not_other_top_level_files(tmp_path):
    """cli.py is a deliberate, narrow exception (the top-level command
    dispatcher must be able to import r3.firewall to expose `kmt
    r3-check`) -- but that exception must not blanket-allow every
    top-level file, only the one named cli.py."""
    cli_stub = tmp_path / "cli.py"
    cli_stub.write_text("from kalshi_mt.r3.firewall import require_r2_locked\n", encoding="utf-8")

    other_top_level = tmp_path / "other.py"
    other_top_level.write_text("from kalshi_mt.r3.firewall import require_r2_locked\n", encoding="utf-8")

    violations = check_no_r3_imports_outside_r3(tmp_path)

    assert violations == [str(other_top_level)]


def test_check_no_r3_imports_outside_r3_catches_absolute_import(tmp_path):
    clean = tmp_path / "clean.py"
    clean.write_text("from kalshi_mt.r2 import verdicts\n", encoding="utf-8")

    dirty = tmp_path / "dirty.py"
    dirty.write_text("import kalshi_mt.r3\n", encoding="utf-8")

    violations = check_no_r3_imports_outside_r3(tmp_path)

    assert violations == [str(dirty)]


def test_check_no_r3_imports_outside_r3_catches_plain_package_import(tmp_path):
    """`from kalshi_mt import r3` -- a valid, real import that doesn't
    literally contain the substring "kalshi_mt.r3", so a naive pattern
    list can miss it even though `import kalshi_mt.r3` is caught."""
    clean = tmp_path / "clean.py"
    clean.write_text("from kalshi_mt import util\n", encoding="utf-8")

    dirty = tmp_path / "dirty.py"
    dirty.write_text("from kalshi_mt import r3\n", encoding="utf-8")

    dirty_aliased = tmp_path / "dirty_aliased.py"
    dirty_aliased.write_text("from kalshi_mt import r3 as r3_pkg\n", encoding="utf-8")

    violations = check_no_r3_imports_outside_r3(tmp_path)

    assert set(violations) == {str(dirty), str(dirty_aliased)}


def test_check_no_r3_imports_outside_r3_catches_relative_forms(tmp_path):
    subpkg = tmp_path / "r2"
    subpkg.mkdir()
    (subpkg / "__init__.py").write_text("", encoding="utf-8")

    from_dotdot_import = subpkg / "a.py"
    from_dotdot_import.write_text("from .. import r3\n", encoding="utf-8")

    from_dotdot_module = subpkg / "b.py"
    from_dotdot_module.write_text("from ..r3 import firewall\n", encoding="utf-8")

    top_level_from_dot_import = tmp_path / "c.py"
    top_level_from_dot_import.write_text("from . import r3\n", encoding="utf-8")

    top_level_from_dot_module = tmp_path / "d.py"
    top_level_from_dot_module.write_text("from .r3 import firewall\n", encoding="utf-8")

    clean = subpkg / "e.py"
    clean.write_text("from ..fees import schedule\n", encoding="utf-8")

    violations = check_no_r3_imports_outside_r3(tmp_path)

    assert set(violations) == {
        str(from_dotdot_import),
        str(from_dotdot_module),
        str(top_level_from_dot_import),
        str(top_level_from_dot_module),
    }


def test_check_no_r3_imports_outside_r3_excludes_files_inside_r3(tmp_path):
    r3_dir = tmp_path / "r3"
    r3_dir.mkdir()
    (r3_dir / "__init__.py").write_text("", encoding="utf-8")
    sibling_ref = r3_dir / "pipeline.py"
    sibling_ref.write_text("import kalshi_mt.r3.firewall\n", encoding="utf-8")

    violations = check_no_r3_imports_outside_r3(tmp_path)

    assert violations == []


def test_check_no_r3_imports_outside_r3_default_src_root_matches_real_tree():
    default_root = Path(__file__).resolve().parents[1] / "src" / "kalshi_mt"
    assert check_no_r3_imports_outside_r3(default_root) == check_no_r3_imports_outside_r3()
