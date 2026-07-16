# Kalshi Makers & Takers — Replication and Extension Spec

**v1.1, 2026-07-15.** Changes from v1.0 after methodological review: category-composition confound promoted from footnote to the central R2 design problem (frozen-mix reweighting + within/between decomposition); R2 verdicts now bind to formal interaction tests, not per-window comparisons; Polymarket added as a secular-trend control venue (source and caveats stated precisely); fee handling upgraded (primary-artifact sourcing, return-convention pin, three-layer decomposition, sensitivity ribbon with a pre-registered "fragile" rule); R1 gated on count reconciliation before any estimate comparison; two-pass fetch design (the boundary-ticks optimization is correct for the price panel and would silently destroy the maker/taker tape if applied globally — pass 2 pulls the full tape for in-scope contracts only); ~30 implementation ambiguities pinned. **Status:** implementation brief for a NEW, separate repository (`kalshi-makers-takers-replication`). Not part of polymarket-forecast-lab — no changes to the lab repo, its ledger, or its confirmatory window.

**Source paper (pinned from the primary PDF, karlwhelan.com/Papers/Kalshi.pdf, January 2026 version):** Bürgi, C., Deng, W. & Whelan, K., *Makers and Takers: The Economics of the Kalshi Prediction Market.* Circulates as CEPR DP 20631 (2025-09-08), CESifo WP 12122, UCD WP2025_19, MPRA 126350. Working paper, not yet journal-published — **check journal status immediately before our submission**; cite the January 2026 version. VoxEU popularization: 2026-02-18 (descriptive marker only, not a test boundary).

---

## 0. What this is and why it works

BDW provide the first systematic evidence on Kalshi pricing (2021 → April 2025): a strong favorite–longshot bias with a maker/taker asymmetry. Two facts make a replication-plus-extension unusually valuable rather than routine:

1. **Their sample cutoff is a fee-regime boundary, not an arbitrary date.** They stop at April 2025 explicitly because "Kalshi began to charge fees on Makers after April 2025." Everything after their window lives under a treatment that directly taxes the paper's headline exploitable margin (maker returns of +2.6% on contracts ≥50c).
2. **They explicitly invite the persistence test** (their Section 6): "We think it will be interesting to see if the biases and return patterns that we have reported persist now that they have been publicly documented." The maker-fee change (2025-05-01, operationalized) and first public posting (CEPR DP, 2025-09-08) are ~4 months apart — two treatments whose timing separation permits a coarse decomposition.

Positioning: the prediction-market twin of paper #1's C2 frame (post-publication persistence, McLean & Pontiff 2016), on a different venue, a different anomaly, and on purely historical data — completable on its own schedule.

**The central design problem, stated up front:** sports launched on Kalshi in early 2025 — almost exactly at the R1/R2 boundary — volumes jumped, and BDW's own Table 8 shows ψ is category-heterogeneous (weak/insignificant for politics and entertainment). Any post-boundary movement in an *aggregate* ψ path is therefore confounded with composition shift. Everything in §2's R2 design exists to separate within-category bias change from between-category composition change; an R2 that reports only the aggregate path is not publishable and will not be run.

## 1. Pinned facts from the source paper (all from the PDF; every count carries its basis tag)

**Sample & construction.** Kalshi inception 2021 → April 2025. Transaction-level data via Kalshi's API (they registered for access). Filters: total traded volume at closure ≥ $1,000; final bid–ask spread ≤ 20c; market open ≥ 24 hours (excludes hourly reset crypto/index series); 63 Yes contracts dropped for mismatch vs Kalshi's separately-reported final prices [pin the exact field behind "separately-reported" during implementation — settlement price vs last price]. Result: **12,403 events, 46,282 Yes contracts** [Yes-only basis]. Prices: last trade on closing day plus last trade before the same time on each of up to 10 prior days → **156,986 Yes prices** [Yes-only basis — the regression n]; descriptives double to **313,972** by adding the No side [doubled basis]. Tail concentration: **106,209 in 1–10c and 106,209 in 90–99c** [doubled basis — symmetric by construction; do NOT reconcile these against a Yes-only build].

**Headline quantities to reproduce (R1 targets).**
- Win-rate-vs-price curve (Fig. 3): FLB — low-price contracts win less than price implies, high-price more.
- Returns by 10c band (Fig. 5): ≤10c lose >60%; small positive above 50c; significantly positive post-fee above 70c; average pre-fee return ≈ **−20%**.
- Maker/taker split (Fig. 6, Table 10): **Makers −9.64%, Takers −31.46%**; makers ≥50c earn **+2.6%**. Maker share 43.5% (1–10c) → 56.5% (90–99c) [doubled basis — the two endpoints are complementary bins summing to ~100% by construction; a Yes-only build will not and should not match them, and such a mismatch is NOT logged as "diverged"].
- Mincer–Zarnowitz `(Y−P) = α + ψP + ε` in cents, Yes-only, SEs clustered at event level (see §4 on why "two-way (event, contract)" reduces to this): full sample **ψ = 0.034 (0.005), α = −1.736 (0.153)**, n = 156,986.
- By-year ψ (Table 9): 2021 **0.041***, 2022 **0.023**\*\*, 2023 **0.036***, 2024 **0.048***, 2025 Jan–Apr **0.021*** (10% only) — their own "some evidence the bias is diminishing," which is exactly the informal per-window comparison our design replaces with a formal test.
- ψ by category (Table 8): smaller/insignificant for politics and entertainment — the heterogeneity that drives the composition problem.
- Fee model in their window: takers only, `$0.07·P(1−P)` per contract, order total rounded up to the next cent; makers zero-fee pre-May-2025. Their "≈1.77% at 50c" is an illustration on a 100-contract order — compute fees on actual per-order contract counts, and reproduce their C=100 illustration separately, labeled as such.

**Robustness notes we inherit:** their results are insensitive to cutting at Dec 2024 or excluding sports — useful, but their annual resolution cannot address the composition problem our monthly design faces; do not cite their robustness as covering ours.

## 2. Research questions

### R1 — reproduction (2021 → 2025-04-30)

**Sequential gate before any estimate comparison:** reconcile construction counts against BDW's pinned integers — 12,403 events / 46,282 contracts / 156,986 prices [Yes-only], 106,209+106,209 tails [doubled]. Estimate comparisons mean nothing until coverage is accounted for: divergence on overlapping deterministic data is a *coverage/filter* question, not a sampling question, so **BDW's standard errors are not the tolerance** — report count deltas, then estimate deltas, in that order. Verdict vocabulary (fixed ex ante): **confirmed** (counts reconcile within documented coverage gaps; signs + significance pattern reproduce), **partially confirmed** (pattern reproduces, magnitudes materially different — quantify and attribute to coverage where possible), **diverged** (sign or significance pattern breaks on reconciled data).

### R2 — extension (2025-05-01 → 2026-06-30; the contribution)

**Primary estimand (one, not four):** the two interaction coefficients from a single pooled, category-interacted Mincer–Zarnowitz regression on Yes prices,

`(Y−P) = α_c + ψ_c·P + Σ_b [ α_{b,c}·D_b + δ_{b,c}·(D_b·P) ] + ε`,

with boundaries b ∈ {fee: 2025-05-01, publication: 2025-09-08}, category subscripts c throughout (ψ is a slope — category enters as *interactions*, not fixed effects), event-clustered SEs, wild cluster bootstrap wherever a cell has <50 event clusters. The two primary tests are the composition-weighted averages **δ̄_fee and δ̄_pub, weighted by the frozen calendar-2024 category mix** (the last full pre-sports year; sports consequently carries zero weight in the headline aggregate and is reported as its own stratum alongside it — that is the point, not a limitation). Verdicts bind to these tests, defined before looking:

- **persisted:** fail to reject δ̄=0 AND reject δ̄ = −ψ̄_R1 (full disappearance ruled out);
- **attenuated:** reject δ̄=0 with −ψ̄_R1 < δ̄ < 0;
- **vanished:** fail to reject δ̄ = −ψ̄_R1 AND reject δ̄=0;
- **reversed:** reject δ̄=0 with ψ̄_R1 + δ̄ significantly < 0;
- indeterminate combinations reported as such, no forcing.

**Composition decomposition (mandatory companion to the headline):** for the aggregate path, decompose Δψ_agg = Σ w̄_c·Δψ_c (within) + Σ Δw_c·ψ̄_c (between) and report both components; any narrative sentence about "the bias" refers to the within component only.

**Control venue (secular-trend check, not a DiD):** overlay Polymarket's monthly ψ path over 2025-05 → 2025-12, computed from the SII-WANGZJ archive (which ends 2025-12-31 — covering exactly the clean window before Polymarket's own staggered 2026 fee reform). Same MZ spec, Polymarket categories mapped to the nearest Kalshi strata. Three caveats stated in the paper, not discovered by referees: (a) Polymarket's tail bias is *reversed* (Qin & Yang 2026), so this controls for market-wide secular efficiency drift, not for level or sign — parallel-trends language is off the table; (b) archive provenance disclosed (this is a different repository and a different use — the market's own calibration path, not model skill — so the lab's archive guardrail does not apply, but say why); (c) spillover: BDW's publication could in principle move Polymarket too via cross-venue arbitrageurs — if the control shows a comparable δ at the same dates, the secular-trend explanation gains weight and we say so.

**Secondary (descriptive, no verdict vocabulary, no escalation power on its own):** maker/taker return gap and the maker ≥50c margin over time, in the three fee layers of §4; tail-bin (≤10c) loss rate path; monthly ψ paths per category.

**Horizon robustness (labeled):** the 10-daily-lookback panel pools horizons; re-estimate (i) horizon-stratified ψ and (ii) a one-observation-per-contract closing-price spec — a δ that survives both is not horizon-composition drift.

### R3 — prospective arm (optional, firewalled)

From 2026-07-04 the lab's live Kalshi collection adds order-book depth/spread covariates BDW's tape lacks. Small n; labeled supplement only. **Firewall rule: R2's verdict is computed and locked in `analysis_plan.md`-referenced output before any R3 number is examined; R2 prose is not revised in light of R3.** R3's window mechanically overlaps R2's tail — the firewall exists because of that overlap.

## 3. Data plan

- **Source:** Kalshi public market-data API (`trade-api/v2`): settled markets via `/markets` (status filter, cursor pagination); trades via `/trades` (includes `taker_side`); candlestick/quote history for the spread filter — **[VERIFY] that historical bid/ask (yes_bid/yes_ask) is retrievable for closed markets**, since the ≤20c final-spread filter cannot be built from the trade tape alone; if quotes are unavailable historically, document the filter substitution explicitly and quantify its effect on counts.
- **Step zero — access check (hard gate):** BDW *registered* for API access. Verify whether the required endpoints serve full history without authentication, **including a positive check: pull 3–5 known 2021–2022 markets end-to-end** — do not infer depth from the endpoint merely responding. If any required endpoint needs an account/API key: **STOP and surface the decision** to the operator. Do not silently register.
- **Two-pass fetch (this is where a naive budget optimization would break the paper):**
  - *Pass 1 — price panel + filters, whole universe:* metadata for all settled markets in scope; then, per contract, only the ~11 boundary last-trades needed for the daily-lookback panel, using `min_ts`/`max_ts` bracketing on `/trades` **[VERIFY the endpoint supports timestamp bracketing]**. Cheap, enables all filters and the MZ panel.
  - *Pass 2 — full trade tape, in-scope contracts only:* complete `/trades` history for contracts passing the §1 filters (~12k events in the R1 era, plus the R2-era analogue). The maker/taker estimands require every fill's price, count, and `taker_side`; boundary ticks cannot substitute. The ≥$1k volume filter selects exactly the heavy-tape markets, so pass 2 is the real budget — schedule it after pass 1's filter results bound its size.
  - Politeness: ≤5 req/s sustained, exponential backoff on 429/5xx, resumable cursors, **freeze timestamp recorded per pass + per-market trade-count reconciliation** (recorded count vs fetched count) as the completeness contract.
- **Storage:** raw JSON → Parquet partitioned by month + SQLite index of markets/events/series.
- **Fee history (`fees.yaml`, right-continuous step function keyed on trade fill timestamp — never seeded from the current row backwards):** pre-2025-05 taker `0.07·P(1−P)` ceil-to-cent per order total (from the paper). **[VERIFY] the post-April-2025 maker-fee schedule and all later revisions from primary artifacts: Kalshi's fee-schedule document version history and its CFTC DCM self-certification rule filings (legal effective dates), with archived copies stored in the repo.** The entire R2 fee-layer analysis is only as credible as this file; it gets its own tests.
- **Construction pins (each changes n silently if left ambiguous):** all "same time" comparisons in **US Eastern Time**; on a no-trade lookback day, **skip** (no backfill) — matching a last-trade-*before-time* construction; "closing day" = calendar ET date of market close timestamp; the 63-mismatch analogue keyed to the pinned settlement-price field; hourly-reset series excluded by series metadata (open duration <24h), not by hand-list.
- **Event/series taxonomy:** map Kalshi `series`/`event`/`market` to BDW's event/contract units; category mapping table versioned in-repo.

## 4. Analysis discipline

- Separate repo, MIT, lab-grade engineering standards; tests mandatory on: fee rounding (per-order ceil on actual counts), gross↔net return reconstruction at P ∈ {0.05, 0.50, 0.95}, clustering equivalence (below), panel construction determinism.
- **Return convention, pinned once:** for a buyer of side s at price P with per-contract fee f, `r = (payout − P − f) / P`. Never subtract a per-notional fee from a per-capital return — the resulting 1/P bias is ≈20× at 5c and ≈2× at 50c, i.e., concentrated exactly on the tail bins and the ≥50c threshold the headline depends on.
- **Three fee layers for every maker/taker quantity:** (a) gross/zero-fee, (b) net of own-era fees, (c) fee-held-constant counterfactual (pre-2025 schedule applied to post-2025 trades). Persistence narrative reads off (a) and (c); layer (b) alone conflates fee incidence with behavioral change.
- **Fee-sensitivity ribbon (detectability mechanism, pre-committed):** recompute the maker ≥50c and >70c margins on a fee grid — zero / BDW taker formula / plausible maker-fee bounds — and report the **break-even fee rate** that zeroes each margin as a headline robustness statistic. **Pre-registered rule: if a margin's sign flips inside the plausible fee band, that result is labeled "fragile" and cannot trigger escalation.**
- **Clustering:** with contracts nested in events, Cameron–Gelbach–Miller two-way (event, contract) variance reduces algebraically to one-way event clustering (V = V_event + V_contract − V_intersection; nesting makes the intersection = contract). Document this, verify numerically once, and use one-way event clustering as the stated spec — no "appears once" hedging. Wild cluster bootstrap for any estimate on <50 event clusters (thin monthly × category cells in R2 will hit this).
- **Basis-tagging rule:** every count and share in the paper carries its basis ([Yes-only] / [doubled]) — §1's pinned numbers already do; the reproduction tables must too.
- **Commit `docs/analysis_plan.md` (§2 expanded to equations and thresholds) BEFORE computing any R2 estimate.** Honest framing: historical data → specification commitment, not outcome-blind pre-registration; R3 is the only genuinely prospective arm; say exactly that in the paper.

## 5. Venue & escalation

- Default: **replication note** — IJF replication section or IREE.
- **Escalation rule, bound to the same tests as the verdicts (no informal "materially shifted" language):** escalate to a standalone short paper ("Do prediction-market anomalies survive fees and publication? Evidence from Kalshi" — Economics Letters / Finance Research Letters class) **iff** δ̄_fee or δ̄_pub rejects zero at 5% under the primary composition-weighted test, **or** the maker ≥50c margin changes sign in layers (a)/(c) *and* survives the entire fee-sensitivity ribbon (the "fragile" rule above withholds escalation otherwise). The McLean–Pontiff framing carries over from paper #1's Related Work nearly verbatim.
- Either way: cite BDW's Section 6 invitation in the introduction — it does for this paper what Le's "designed to be re-estimated" line does for paper #1.

## 6. Timeline (pinned, with the v1.0 contradiction resolved)

**R2 window is 2025-05-01 → 2026-06-30** (resolved-by-fetch, with a two-week resolution-lag buffer at the right edge) — consistent with a mid-August draft. **Refresh branch:** if escalation fires, the window refreshes to 2026-12-31 before submission of the standalone paper, and the draft timeline moves accordingly; the note format does not refresh.

| When | What |
|---|---|
| Days 1–3 | Step-zero access gate incl. 2021–2022 positive check → pass-1 fetch on VPS |
| Week 1 | Filters + count reconciliation vs BDW's integers (the R1 gate); pass-2 fetch sized and launched |
| Before any R2 number | `analysis_plan.md` committed (equations, thresholds, verdict definitions, escalation inequality) |
| Weeks 2–3 | R1 reproduction tables + divergence log; fee YAML sourced from primary artifacts |
| Weeks 3–4 | R2: primary interaction tests, decomposition, control-venue overlay, fee layers + ribbon; escalation decision per §5 |
| ~Mid-August 2026 | Full draft (note format) |

## 7. Known risks, restated

- **Historical API completeness** (2021–2022 depth) — step-zero positive check; if partial, R1 becomes a partial-window reproduction with coverage reported against 46,282.
- **Fee-history reconstruction** — still the single most error-prone input; now mitigated by primary-artifact sourcing, the step-function keying, the three layers, and the ribbon with its break-even statistic.
- **Composition drift** — now the design's centerpiece rather than a risk footnote; residual risk is thin category cells (wild bootstrap + honest "insufficient cell" reporting).
- **Scoop risk** — BDW invited the question in print and own a pipeline; speed is the moat; check SSRN/arXiv for BDW follow-ups before drafting.
- **Control-venue validity** — reversed-sign bias and spillover caveats disclosed by design (§2); the control informs, it does not identify.

---

*Placeholder inventory: [VERIFY] historical bid/ask availability for the spread filter; [VERIFY] `/trades` timestamp bracketing; [VERIFY] post-April-2025 maker-fee schedule from Kalshi fee-document version history + CFTC self-certification filings (archive copies in-repo); step-zero access result; BDW journal status at submission; R3 inclusion decision in August (after R2 verdict lock); exact settlement-price field behind the 63-mismatch filter; Polymarket→Kalshi category mapping table version.*
