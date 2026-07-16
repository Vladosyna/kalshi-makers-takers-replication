# Analysis Plan

**Committed:** 2026-07-16 (UTC). **Status:** primary document — first version, no addenda yet.

This document expands `Claude.md` (v1.1) §2/§4/§5 into exact equations, thresholds, and inequalities, committed once, before any R2 estimate is computed — per the spec's own requirement ("Commit `docs/analysis_plan.md` ... BEFORE computing any R2 estimate") and this repo's honesty framing: historical data means this is **specification commitment**, not outcome-blind pre-registration. R3 (§4 below) is the only genuinely prospective arm. Any later change is appended as a dated addendum (§7), never a silent rewrite of §1–§6 — the same append-only discipline the sibling lab's `docs/pre_analysis_plan.md` follows.

No R2 data has been analyzed as of this commitment — Phase 2 (fetch) is in progress and Phase 3 (R1 construction) has not run.

---

## 1. R1 — reproduction (2021 → 2025-04-30)

**Sequential gate**, before any estimate comparison: reconcile construction counts against BDW's pinned integers — 12,403 events / 46,282 Yes contracts / 156,986 Yes prices [Yes-only basis], 106,209+106,209 tail counts [doubled Yes+No basis]. Divergence on overlapping deterministic data is a coverage/filter question, not a sampling question — BDW's own standard errors are **not** the tolerance for this gate. Count deltas are reported first; estimate deltas (ψ, α, maker/taker split, etc.) are compared only after coverage is accounted for.

**Verdict vocabulary**, fixed here, applied per quantity:

- **confirmed** — counts reconcile within a documented coverage gap, AND sign + significance pattern reproduce.
- **partially confirmed** — pattern reproduces, magnitudes are materially different (quantified, attributed to coverage where possible).
- **diverged** — sign or significance pattern breaks on reconciled data.

## 2. R2 — extension (2025-05-01 → 2026-06-30)

### 2.1 Primary estimand

One pooled, category-interacted Mincer–Zarnowitz regression on Yes prices (cents), event-clustered standard errors, wild cluster bootstrap for any cell with fewer than 50 event clusters:

```
(Y − P) = α_c + ψ_c·P + Σ_b [ α_{b,c}·D_b + δ_{b,c}·(D_b·P) ] + ε
```

- `c` — category (interactions, not fixed effects, since ψ is a slope).
- `b ∈ {fee, publication}` — boundary dummies: `fee` = 1 on/after 2025-05-01, `publication` = 1 on/after 2025-09-08.
- The two primary test statistics are the **composition-weighted averages** `δ̄_fee` and `δ̄_pub`, weighted by the **frozen calendar-2024 category mix** (`data/frozen_2024_mix.json`, produced once during Phase 3's R1 construction from 2021–2025-04-30 data — an R1-window artifact, never recomputed from R2 data; Phase 7 refuses to run without it). Sports carries zero weight in this headline aggregate by construction (it did not exist in 2024) and is reported as its own stratum alongside it.

### 2.2 Verdict definitions (bind to formal tests, not per-window comparison)

Let `ψ̄_R1` be R1's reproduced full-sample ψ (§1) and its confidence interval. Verdicts are computed from a **cluster-robust test on the interaction coefficient `δ̄`** in the pooled regression above — never by comparing significance stars across separately-estimated per-window regressions (the informal trap BDW's own "diminishing bias" language falls into, and the trap this repo's methodological review flagged explicitly):

| Verdict | Condition |
|---|---|
| **persisted** | fail to reject `δ̄ = 0` AND reject `δ̄ = −ψ̄_R1` (full disappearance ruled out) |
| **attenuated** | reject `δ̄ = 0`, with `−ψ̄_R1 < δ̄ < 0` |
| **vanished** | fail to reject `δ̄ = −ψ̄_R1` AND reject `δ̄ = 0` |
| **reversed** | reject `δ̄ = 0`, with `ψ̄_R1 + δ̄` significantly `< 0` |
| **indeterminate** | none of the above cleanly hold — reported as such, never forced into the nearest label |

Applied separately to `δ̄_fee` and `δ̄_pub`.

### 2.3 Composition decomposition (mandatory companion to the headline)

```
Δψ_agg = Σ_c w̄_c · Δψ_c        (within)
       + Σ_c Δw_c · ψ̄_c        (between)
```

weights `w_c` from the frozen 2024 mix (§2.1). Any narrative sentence about "the bias" changing refers to the **within** component only; the between component is reported alongside, never folded into the headline claim.

### 2.4 Control venue (secular-trend check, not a difference-in-differences)

Polymarket's own monthly ψ path, same MZ spec, over **2025-05 → 2025-12 only** — computed from the SII-WANGZJ archive, which ends 2025-12-31. **Coverage gap, stated plainly:** R2's final ~6 months (2026-01 → 2026-06) have **no control venue**. Extending the archive is not merely unavailable — Polymarket's own 2026 fee reform begins January 2026 (crypto Jan, sports Feb, other categories Mar, per the sibling lab's sourced `data/fee_schedule.yaml`), so even a hypothetically longer archive would contaminate the control with Polymarket's own treatment inside this window. The Kalshi-vs-Polymarket differential is reported only over the covered sub-window; the later period is described as uncontrolled, not silently extrapolated.

Three caveats carried verbatim into any write-up:

1. Polymarket's tail bias is *reversed* relative to Kalshi's (Qin & Yang 2026) — this control informs market-wide secular efficiency drift, not level or sign; "parallel trends" language is not used.
2. Archive provenance: a different repository, a different use (the market's own calibration path, not a model-skill claim) — the sibling lab's own archive-usage guardrail does not apply here, and this line states why rather than leaving it to be inferred.
3. Spillover: BDW's publication could in principle move Polymarket too via cross-venue arbitrageurs. If the control shows a comparable δ at the same dates, that is reported as strengthening the secular-trend explanation, not as a defect in the design.

### 2.5 Secondary / descriptive (no verdict vocabulary, no escalation power on their own)

- Maker/taker return gap and the maker ≥50c margin over time, in the three fee layers (§3.2).
- Tail-bin (≤10c) post-fee loss rate path.
- Monthly ψ paths per category (illustration/descriptive; the composition-weighted `δ̄` tests in §2.2 are the inferential object, not a scan over these monthly points).

### 2.6 Horizon robustness (labeled, not a headline estimand)

The 10-daily-lookback panel pools observations across up to 11 horizons-to-close per contract. Re-estimate:

1. horizon-stratified ψ (ψ per lookback-day bucket), and
2. a one-observation-per-contract closing-price-only spec.

A `δ̄` that survives both is treated as evidence against pure horizon-composition drift; one that doesn't is reported as such, not discarded.

## 3. Fee handling

### 3.1 Return convention (pinned once)

For a buyer of side *s* at price *P* with per-contract fee *f*:

```
r = (payout − P − f) / P
```

Never subtract a per-notional fee from a per-capital return — the resulting 1/P bias is ≈20× at 5c and ≈2× at 50c, concentrated exactly on the tail bins and the ≥50c threshold the R2 headline depends on.

### 3.2 Three fee layers, every maker/taker quantity

| Layer | Definition |
|---|---|
| (a) gross / zero-fee | no fee subtracted |
| (b) net of own-era fees | the fee schedule in force on the trade's own fill date (`data/fees.yaml`) |
| (c) fee-held-constant counterfactual | the pre-2025-05 (taker-only) schedule applied to post-2025 trades |

The persistence narrative reads off layers **(a) and (c)**; layer (b) alone conflates fee incidence with behavioral change and is reported alongside, not as the headline.

### 3.3 Fee-sensitivity ribbon (pre-registered detectability rule)

For every fee-conditional R2 headline (tail-bin post-fee loss, maker/taker gap, maker ≥50c margin): recompute across a fee grid {zero-fee; BDW's pre-2025 taker formula `0.07·P·(1−P)`; the sourced post-2025 maker/taker rates ± a plausible band}, and report the **break-even fee rate** that drives each margin to zero.

**Pre-registered rule:** if a margin's sign flips anywhere inside the plausible fee band, that result is labeled **fragile** in the write-up, and — per §5 below — cannot trigger escalation on its own.

## 4. R3 — prospective arm (firewalled)

From 2026-07-04, the lab's live Kalshi collection adds order-book depth/spread covariates BDW's own tape lacks. Small *n*; labeled supplement only, never pooled with R1/R2's own estimates.

**Firewall rule:** R2's verdict (§2.2) is computed and locked to its own output artifact **before** any R3 number is examined. R2's narrative is not revised in light of R3, even if R3 is suggestive. This rule exists specifically because R3's window (from 2026-07-04) mechanically overlaps R2's tail (through 2026-06-30) — without a firewall, an analyst could let R3's early signal color how an ambiguous R2 verdict gets written up.

## 5. Escalation rule (bound to §2.2's tests, no informal language)

Escalate from a replication note to a standalone short paper **iff**:

```
( δ̄_fee  rejects zero at 5%, under the primary composition-weighted test )
                              OR
( δ̄_pub  rejects zero at 5%, under the primary composition-weighted test )
                              OR
( the maker ≥50c margin changes sign between layers (a) and (c)
  AND survives the entire fee-sensitivity ribbon — i.e. is NOT labeled "fragile" per §3.3 )
```

No other trigger. "Materially regime-shifted" is not used as a standalone justification anywhere in this repo's write-up — every escalation claim traces to one of the three conditions above.

## 6. Honesty framing (stated once, applies throughout)

This is **specification commitment**, not outcome-blind pre-registration: the data (2025-05 → 2026-06-30) already exists on Kalshi's servers as of this document's commitment date, even though this repo has not yet fetched or examined it (Phase 2 fetch is in progress; Phase 3 R1 construction, which the R2 pipeline in §2 depends on for the frozen 2024 mix, has not run). R3 (§4) is the only genuinely prospective arm — its data literally does not exist yet for markets closing after this document's commitment date. The verdict thresholds in §2.2, the fee layers in §3, and the escalation rule in §5 are fixed now, before any R2 estimate exists, specifically to remove analyst discretion at write-up time.

## 7. Addenda

*(none yet)*
