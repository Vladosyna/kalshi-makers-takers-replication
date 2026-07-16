from __future__ import annotations

import pytest

from kalshi_mt.r2.decomposition import DecompositionResult
from kalshi_mt.r2.horizon import HorizonBucketResult, HorizonRobustnessResult
from kalshi_mt.r2.report import build_r2_report, load_r2_report, write_r2_report
from kalshi_mt.r2.verdicts import DeltaBarEstimate


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
# build_r2_report
# ---------------------------------------------------------------------------

def test_build_r2_report_shape():
    report = build_r2_report(**_sample_report_kwargs())
    assert report["psi_bar_r1"] == 0.041
    assert report["categories_fit"] == ["Politics", "Weather"]
    assert report["delta_bar"]["fee"] == {"delta_bar": -1.0, "ci_lo": -1.5, "ci_hi": -0.5}
    assert report["delta_bar"]["publication"] is None
    assert report["verdict"]["fee"] == "attenuated"
    assert report["decomposition"]["fee"]["within"] == -1.0
    assert report["horizon_robustness"]["close_only"]["lookback_day"] == 0
    assert "locked_ts" not in report  # build_r2_report is pure assembly, no I/O, no clock


# ---------------------------------------------------------------------------
# write_r2_report / load_r2_report roundtrip
# ---------------------------------------------------------------------------

def test_write_then_load_roundtrip(tmp_path):
    report = build_r2_report(**_sample_report_kwargs())
    path = write_r2_report(report, tmp_path / "r2" / "verdict_lock.json")
    assert path.exists()

    loaded = load_r2_report(path)
    assert "locked_ts" in loaded
    assert loaded["psi_bar_r1"] == 0.041
    assert loaded["verdict"]["fee"] == "attenuated"


def test_write_r2_report_creates_parent_dirs(tmp_path):
    report = build_r2_report(**_sample_report_kwargs())
    path = write_r2_report(report, tmp_path / "deep" / "nested" / "dir" / "verdict_lock.json")
    assert path.exists()


def test_load_r2_report_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="is missing"):
        load_r2_report(tmp_path / "nope.json")


def test_load_r2_report_missing_locked_ts_raises(tmp_path):
    path = tmp_path / "fake.json"
    path.write_text('{"psi_bar_r1": 0.5}', encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="locked_ts"):
        load_r2_report(path)


def test_write_r2_report_does_not_mutate_input_dict(tmp_path):
    report = build_r2_report(**_sample_report_kwargs())
    original_keys = set(report.keys())
    write_r2_report(report, tmp_path / "verdict_lock.json")
    assert set(report.keys()) == original_keys
    assert "locked_ts" not in report
