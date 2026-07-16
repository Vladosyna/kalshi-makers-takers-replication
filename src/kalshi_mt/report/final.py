"""Final report assembly (Claude.md S5; docs/analysis_plan.md S5): a
markdown draft skeleton pulling together every upstream artifact -- R1
reproduction, R2's locked verdict (S2.2), decomposition (S2.3), horizon
robustness (S2.6), the Polymarket control venue (S2.4) if available, the
maker >=50c margin and its fee-sensitivity ribbon (S3.2/S3.3), and the
escalation determination (S5) -- into the note-format (default) or
standalone-short-paper (if escalated) shell Claude.md S5 describes.

Venue is READ OFF the escalation result, never asserted independently:
'replication note' (IJF replication section or IREE) by default,
'standalone short paper' (title/venue-class below, McLean-Pontiff framing)
iff EscalationResult.escalate is True. BDW's Section 6 invitation is cited
in the introduction either way (Claude.md S5's own instruction) -- it is
not conditional on escalation, unlike the venue choice.

This module does no I/O except the final write (write_final_report) and no
computation of its own beyond formatting -- every number it renders is
computed elsewhere and passed in already-finished (r2_report from
kalshi_mt.r2.report.load_r2_report, escalation from
kalshi_mt.report.escalation.determine_escalation, etc.), matching
report/escalation.py's own "pure function of already-computed artifacts"
discipline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kalshi_mt.fees.ribbon import RibbonResult
from kalshi_mt.r2.maker_margin import MakerMarginResult
from kalshi_mt.report.escalation import EscalationResult
from kalshi_mt.util import now_utc_iso

BDW_SECTION_6_QUOTE = (
    "We think it will be interesting to see if the biases and return "
    "patterns that we have reported persist now that they have been "
    "publicly documented."
)

STANDALONE_PAPER_TITLE = "Do prediction-market anomalies survive fees and publication? Evidence from Kalshi"
STANDALONE_PAPER_VENUE_CLASS = "Economics Letters / Finance Research Letters class"
REPLICATION_NOTE_VENUES = ["IJF replication section", "IREE"]


def determine_venue(escalation: EscalationResult) -> dict[str, Any]:
    """Claude.md S5: default is a replication note; escalate to the
    standalone short paper shell iff the escalation rule fired. The venue
    dict always carries `triggers` so the report can explain WHY, even on
    the default path (empty list there)."""
    if escalation.escalate:
        return {
            "kind": "standalone_short_paper",
            "title": STANDALONE_PAPER_TITLE,
            "venue_class": STANDALONE_PAPER_VENUE_CLASS,
            "triggers": list(escalation.triggers),
        }
    return {
        "kind": "replication_note",
        "candidate_venues": list(REPLICATION_NOTE_VENUES),
        "triggers": [],
    }


def _fmt(x: float | None, digits: int = 4) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def _fmt_pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def _render_delta_bar_row(label: str, entry: dict[str, Any] | None, verdict: str | None) -> list[str]:
    if entry is None:
        return [f"- **{label}**: not computed (no category carried a nonzero frozen weight)."]
    ci = f"[{_fmt(entry['ci_lo'])}, {_fmt(entry['ci_hi'])}]"
    return [
        f"- **{label}**: {_fmt(entry['delta_bar'])} cents/cents, 95% CI {ci} -- verdict: **{verdict or 'n/a'}**"
    ]


def _render_decomposition(boundary: str, decomp: dict[str, Any]) -> list[str]:
    return [
        f"- **{boundary}**: within = {_fmt(decomp['within'])}, between = {_fmt(decomp['between'])}, "
        f"aggregate = {_fmt(decomp['aggregate'])} "
        "(within is the persistence narrative's own object; between is reported alongside, "
        "never folded into that claim -- docs/analysis_plan.md S2.3)."
    ]


def _render_horizon_robustness(horizon: dict[str, Any]) -> list[str]:
    lines = ["| lookback_day | n | delta_bar_fee | delta_bar_pub | categories fit |", "|---|---|---|---|---|"]
    for bucket in horizon.get("by_bucket", []):
        lines.append(
            f"| {bucket['lookback_day']} | {bucket['n']} | {_fmt(bucket['delta_bar_fee'])} | "
            f"{_fmt(bucket['delta_bar_pub'])} | {bucket['n_categories_fit']} |"
        )
    close_only = horizon.get("close_only")
    lines.append("")
    if close_only is not None:
        lines.append(
            f"One-observation-per-contract spec (S2.6 item 2): n={close_only['n']}, "
            f"delta_bar_fee={_fmt(close_only['delta_bar_fee'])}, delta_bar_pub={_fmt(close_only['delta_bar_pub'])}."
        )
    else:
        lines.append("One-observation-per-contract spec (S2.6 item 2): not computed (empty panel).")
    return lines


def _render_maker_margin(maker_margin: MakerMarginResult | None, ribbon: RibbonResult | None) -> list[str]:
    if maker_margin is None:
        return ["Not computed this run."]
    lines = [
        "| layer | margin (maker - taker, >=50c) | n_maker | n_taker |",
        "|---|---|---|---|",
        f"| (a) gross | {_fmt(maker_margin.layer_a)} | {maker_margin.n_maker_a} | {maker_margin.n_taker_a} |",
        f"| (b) net of own-era fees | {_fmt(maker_margin.layer_b)} | {maker_margin.n_maker_b} | {maker_margin.n_taker_b} |",
        f"| (c) fee-held-constant counterfactual | {_fmt(maker_margin.layer_c)} | {maker_margin.n_maker_c} | {maker_margin.n_taker_c} |",
    ]
    if maker_margin.gap_excluded_b or maker_margin.gap_excluded_c:
        lines.append(
            f"\n(fee-schedule-gap exclusions: {maker_margin.gap_excluded_b} observations dropped from "
            f"layer (b), {maker_margin.gap_excluded_c} from layer (c).)"
        )
    lines.append("")
    if ribbon is None:
        lines.append("Fee-sensitivity ribbon (S3.3): not computed this run.")
    else:
        lines.append(
            f"Fee-sensitivity ribbon (S3.3), swept over the maker rate: break-even rate = "
            f"{_fmt_pct(ribbon.break_even_rate, 2) if ribbon.break_even_rate is not None else 'never crosses zero in-grid'}, "
            f"sign_flips = {ribbon.sign_flips}, **fragile = {ribbon.fragile}** "
            f"(a fragile result cannot trigger escalation on its own, per the pre-registered rule)."
        )
    return lines


def _render_control_venue(control_monthly_psi: list[dict[str, Any]] | None, caveats: list[str], coverage_gap_statement: str) -> list[str]:
    lines = []
    if control_monthly_psi is None:
        lines.append(
            "Not computed this run (Polymarket bootstrap archive not downloaded/scanned -- "
            "see kalshi_mt.control.polymarket.download_bootstrap_files)."
        )
    else:
        lines.append("| month | psi | n | n_clusters |")
        lines.append("|---|---|---|---|")
        for entry in control_monthly_psi:
            result = entry.get("result")
            if result is None:
                lines.append(f"| {entry['month']} | insufficient data | - | - |")
            else:
                lines.append(f"| {entry['month']} | {_fmt(result['psi'])} | {result['n']} | {result['n_clusters']} |")
    lines.append("")
    lines.append("**Mandatory caveats (carried verbatim, docs/analysis_plan.md S2.4):**")
    for i, caveat in enumerate(caveats, start=1):
        lines.append(f"{i}. {caveat}")
    lines.append("")
    lines.append(f"**Coverage gap:** {coverage_gap_statement}")
    return lines


def _render_escalation_detail(escalation: EscalationResult) -> list[str]:
    lines = [f"**Escalate: {escalation.escalate}**", ""]
    if escalation.triggers:
        lines.append("Triggers fired: " + ", ".join(escalation.triggers))
    else:
        lines.append("No trigger condition fired.")
    lines.append("")
    fee = escalation.detail.get("delta_bar_fee", {})
    pub = escalation.detail.get("delta_bar_pub", {})
    margin = escalation.detail.get("maker_margin_sign_flip", {})
    lines += [
        f"- delta_bar_fee rejects zero at 5%: {fee.get('rejects_zero')} "
        f"(available={fee.get('available')}, delta_bar={_fmt(fee.get('delta_bar'))})",
        f"- delta_bar_pub rejects zero at 5%: {pub.get('rejects_zero')} "
        f"(available={pub.get('available')}, delta_bar={_fmt(pub.get('delta_bar'))})",
        f"- maker margin sign-flip (a vs c) surviving the ribbon: {margin.get('condition_met')} "
        f"(layer_a={_fmt(margin.get('layer_a'))}, layer_c={_fmt(margin.get('layer_c'))}, "
        f"sign_flip={margin.get('sign_flip')}, ribbon_fragile={margin.get('ribbon_fragile')})",
    ]
    return lines


def build_final_report_markdown(
    *,
    r2_report: dict[str, Any],
    escalation: EscalationResult,
    maker_margin: MakerMarginResult | None = None,
    ribbon: RibbonResult | None = None,
    control_monthly_psi: list[dict[str, Any]] | None = None,
    control_caveats: list[str] | None = None,
    control_coverage_gap_statement: str | None = None,
    r1_divergence_log_path: str | None = None,
) -> str:
    """Pure rendering -- every input is already-computed data, no I/O, no
    clock call except the report header timestamp (now_utc_iso, this
    module's only clock call, matching the one-clock-call-per-module
    convention the rest of this codebase follows)."""
    venue = determine_venue(escalation)
    verdict = r2_report.get("verdict", {})
    delta_bar = r2_report.get("delta_bar", {})
    decomposition = r2_report.get("decomposition", {})
    horizon = r2_report.get("horizon_robustness", {})

    lines: list[str] = []
    if venue["kind"] == "standalone_short_paper":
        lines.append(f"# {venue['title']}")
        lines.append(f"*({venue['venue_class']})*")
    else:
        lines.append("# Kalshi Makers & Takers -- Replication Note (Draft)")
        lines.append(f"*(candidate venues: {', '.join(venue['candidate_venues'])})*")
    lines.append("")
    lines.append(f"Draft assembled: {now_utc_iso()}")
    lines.append(f"R2 verdict locked: {r2_report.get('locked_ts', 'n/a')}")
    lines.append("")

    lines.append("## 1. Introduction")
    lines.append("")
    lines.append(f"> \"{BDW_SECTION_6_QUOTE}\"")
    lines.append(">")
    lines.append("> -- Burgi, Deng & Whelan, Section 6")
    lines.append("")
    lines.append(
        "This work takes up that invitation: does the favorite-longshot bias and the "
        "maker/taker return asymmetry BDW document persist after their maker-fee change "
        "(2025-05-01) and public posting (2025-09-08)?"
    )
    lines.append("")

    lines.append("## 2. R1 reproduction")
    lines.append("")
    lines.append(f"Reproduced full-sample psi_bar_R1 = {_fmt(r2_report.get('psi_bar_r1'))}.")
    if r1_divergence_log_path:
        lines.append(f"Full by-year/by-category divergence log: {r1_divergence_log_path}")
    lines.append("")

    lines.append("## 3. R2 findings")
    lines.append("")
    lines.append("### 3.1 Primary composition-weighted test (S2.1-S2.2)")
    lines += _render_delta_bar_row("delta_bar_fee", delta_bar.get("fee"), verdict.get("fee"))
    lines += _render_delta_bar_row("delta_bar_pub", delta_bar.get("publication"), verdict.get("publication"))
    lines.append("")
    lines.append("### 3.2 Composition decomposition (S2.3)")
    lines += _render_decomposition("fee boundary", decomposition.get("fee", {}))
    lines += _render_decomposition("publication boundary", decomposition.get("publication", {}))
    lines.append("")
    lines.append(f"Categories fit: {', '.join(r2_report.get('categories_fit', [])) or 'none'}")
    lines.append(
        f"Panel sizes: R1={r2_report.get('r1_panel_n')}, R2={r2_report.get('r2_panel_n')}, "
        f"pooled={r2_report.get('pooled_panel_n')}"
    )
    lines.append("")
    lines.append("### 3.3 Horizon robustness (S2.6)")
    lines.append("")
    lines += _render_horizon_robustness(horizon)
    lines.append("")

    lines.append("## 4. Fee layers and the maker >=50c margin (S3.2-S3.3)")
    lines.append("")
    lines += _render_maker_margin(maker_margin, ribbon)
    lines.append("")

    lines.append("## 5. Control venue: Polymarket (S2.4)")
    lines.append("")
    lines += _render_control_venue(
        control_monthly_psi,
        control_caveats if control_caveats is not None else [],
        control_coverage_gap_statement or "not stated (control venue not computed this run)",
    )
    lines.append("")

    lines.append("## 6. Escalation determination (S5)")
    lines.append("")
    lines += _render_escalation_detail(escalation)
    lines.append("")

    lines.append("## 7. R3 (firewalled, prospective)")
    lines.append("")
    lines.append(
        "R2's verdict above is locked to its own output artifact before any R3 number is "
        "examined (docs/analysis_plan.md S4). This narrative is not revised in light of R3, "
        "even if R3 is suggestive. See kalshi_mt.r3.firewall."
    )
    lines.append("")

    return "\n".join(lines)


def write_final_report(markdown: str, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path
