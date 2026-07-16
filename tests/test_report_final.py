from __future__ import annotations

from kalshi_mt.fees.ribbon import RibbonResult
from kalshi_mt.r2.decomposition import DecompositionResult
from kalshi_mt.r2.horizon import HorizonBucketResult, HorizonRobustnessResult
from kalshi_mt.r2.maker_margin import MakerMarginResult
from kalshi_mt.r2.report import build_r2_report
from kalshi_mt.r2.verdicts import DeltaBarEstimate
from kalshi_mt.report.escalation import EscalationResult, determine_escalation
from kalshi_mt.report.final import (
    REPLICATION_NOTE_VENUES,
    STANDALONE_PAPER_TITLE,
    build_final_report_markdown,
    determine_venue,
    write_final_report,
)


def _sample_r2_report(fee_ci_lo=-1.5, fee_ci_hi=-0.5, fee_verdict="attenuated"):
    horizon = HorizonRobustnessResult(
        by_bucket=[HorizonBucketResult(lookback_day=0, n=50, delta_bar_fee=-1.0, delta_bar_pub=None, n_categories_fit=1)],
        close_only=HorizonBucketResult(lookback_day=0, n=50, delta_bar_fee=-1.0, delta_bar_pub=None, n_categories_fit=1),
    )
    report = build_r2_report(
        r2_filters={"total": 10, "passed": 10, "failed": 0, "reason_counts": {}},
        psi_bar_r1=0.041,
        r1_panel_n=100, r2_panel_n=50, pooled_panel_n=150,
        categories_fit=["Weather"],
        delta_bar={
            "fee": DeltaBarEstimate(delta_bar=-1.0, ci_lo=fee_ci_lo, ci_hi=fee_ci_hi),
            "publication": None,
        },
        verdict={"fee": fee_verdict, "publication": None},
        decomposition={
            "fee": DecompositionResult(within=-1.0, between=0.1, aggregate=-0.9, per_category={}),
            "publication": DecompositionResult(within=0.0, between=0.0, aggregate=0.0, per_category={}),
        },
        horizon=horizon,
    )
    report["locked_ts"] = "2026-07-16T12:00:00+00:00"
    return report


def _no_trigger_escalation():
    return determine_escalation(
        delta_bar_fee=DeltaBarEstimate(delta_bar=-1.0, ci_lo=-1.5, ci_hi=0.5),
        delta_bar_pub=None, maker_margin_layer_a=None, maker_margin_layer_c=None, ribbon=None,
    )


def _fee_trigger_escalation():
    return determine_escalation(
        delta_bar_fee=DeltaBarEstimate(delta_bar=-1.0, ci_lo=-1.5, ci_hi=-0.5),
        delta_bar_pub=None, maker_margin_layer_a=None, maker_margin_layer_c=None, ribbon=None,
    )


# ---------------------------------------------------------------------------
# determine_venue
# ---------------------------------------------------------------------------

def test_determine_venue_replication_note_when_no_escalation():
    venue = determine_venue(_no_trigger_escalation())
    assert venue["kind"] == "replication_note"
    assert venue["candidate_venues"] == REPLICATION_NOTE_VENUES
    assert venue["triggers"] == []


def test_determine_venue_standalone_paper_when_escalated():
    venue = determine_venue(_fee_trigger_escalation())
    assert venue["kind"] == "standalone_short_paper"
    assert venue["title"] == STANDALONE_PAPER_TITLE
    assert venue["triggers"] == ["delta_bar_fee_significant"]


# ---------------------------------------------------------------------------
# build_final_report_markdown -- structural checks
# ---------------------------------------------------------------------------

def test_final_report_cites_bdw_section_6_regardless_of_venue():
    for escalation in (_no_trigger_escalation(), _fee_trigger_escalation()):
        md = build_final_report_markdown(r2_report=_sample_r2_report(), escalation=escalation)
        assert "Section 6" in md
        assert "publicly documented" in md


def test_final_report_replication_note_title_on_default_path():
    md = build_final_report_markdown(r2_report=_sample_r2_report(), escalation=_no_trigger_escalation())
    assert "Replication Note" in md
    assert STANDALONE_PAPER_TITLE not in md


def test_final_report_standalone_title_on_escalated_path():
    md = build_final_report_markdown(r2_report=_sample_r2_report(), escalation=_fee_trigger_escalation())
    assert STANDALONE_PAPER_TITLE in md


def test_final_report_renders_verdict_and_ci():
    md = build_final_report_markdown(r2_report=_sample_r2_report(), escalation=_no_trigger_escalation())
    assert "attenuated" in md
    assert "-1.5000" in md and "-0.5000" in md


def test_final_report_handles_missing_publication_delta_bar():
    md = build_final_report_markdown(r2_report=_sample_r2_report(), escalation=_no_trigger_escalation())
    assert "delta_bar_pub" in md
    assert "not computed" in md


def test_final_report_renders_horizon_table():
    md = build_final_report_markdown(r2_report=_sample_r2_report(), escalation=_no_trigger_escalation())
    assert "lookback_day" in md
    assert "One-observation-per-contract" in md


def test_final_report_maker_margin_not_computed_states_so():
    md = build_final_report_markdown(r2_report=_sample_r2_report(), escalation=_no_trigger_escalation())
    assert "Not computed this run" in md


def test_final_report_maker_margin_and_ribbon_rendered_when_present():
    margin = MakerMarginResult(
        layer_a=0.02, layer_b=-0.01, layer_c=0.02,
        n_maker_a=40, n_taker_a=40, n_maker_b=30, n_taker_b=30, n_maker_c=40, n_taker_c=40,
        gap_excluded_b=5, gap_excluded_c=0,
    )
    ribbon = RibbonResult(
        rates=[0.0, 0.0175], margins=[0.02, -0.01],
        break_even_rate=0.01, sign_flips=True, fragile=True,
    )
    md = build_final_report_markdown(
        r2_report=_sample_r2_report(), escalation=_no_trigger_escalation(),
        maker_margin=margin, ribbon=ribbon,
    )
    assert "0.0200" in md
    assert "fragile = True" in md
    assert "gap-schedule" not in md  # sanity: no typo'd label
    assert "fee-schedule-gap exclusions" in md


def test_final_report_control_venue_not_computed_states_so_with_caveats():
    md = build_final_report_markdown(
        r2_report=_sample_r2_report(), escalation=_no_trigger_escalation(),
        control_caveats=["caveat one", "caveat two"],
        control_coverage_gap_statement="coverage gap text",
    )
    assert "Not computed this run" in md
    assert "caveat one" in md
    assert "caveat two" in md
    assert "coverage gap text" in md


def test_final_report_control_venue_monthly_table_rendered():
    monthly = [
        {"month": "2025-05", "result": {"psi": 1.2, "n": 30, "n_clusters": 30}},
        {"month": "2025-06", "result": None},
    ]
    md = build_final_report_markdown(
        r2_report=_sample_r2_report(), escalation=_no_trigger_escalation(),
        control_monthly_psi=monthly, control_caveats=[], control_coverage_gap_statement="gap",
    )
    assert "2025-05" in md
    assert "1.2000" in md
    assert "insufficient data" in md


def test_final_report_escalation_detail_lists_triggers():
    md = build_final_report_markdown(r2_report=_sample_r2_report(), escalation=_fee_trigger_escalation())
    assert "delta_bar_fee_significant" in md
    assert "Escalate: True" in md


def test_final_report_no_trigger_states_none_fired():
    md = build_final_report_markdown(r2_report=_sample_r2_report(), escalation=_no_trigger_escalation())
    assert "Escalate: False" in md
    assert "No trigger condition fired" in md


def test_final_report_mentions_r3_firewall():
    md = build_final_report_markdown(r2_report=_sample_r2_report(), escalation=_no_trigger_escalation())
    assert "R3" in md
    assert "firewall" in md.lower()


# ---------------------------------------------------------------------------
# write_final_report
# ---------------------------------------------------------------------------

def test_write_final_report_creates_parent_dirs_and_persists(tmp_path):
    md = build_final_report_markdown(r2_report=_sample_r2_report(), escalation=_no_trigger_escalation())
    path = write_final_report(md, tmp_path / "deep" / "final" / "draft.md")
    assert path.exists()
    assert path.read_text(encoding="utf-8") == md
