# Kalshi Makers & Takers — Replication and Extension Spec

**Status:** implementation brief for a NEW, separate repository (`kalshi-makers-takers-replication`). Not part of polymarket-forecast-lab — no changes to the lab repo, its ledger, or its confirmatory window. **Date:** 2026-07-14, v1.0.

**Source paper (pinned from the primary PDF, karlwhelan.com/Papers/Kalshi.pdf, January 2026 version):** Bürgi, C., Deng, W. & Whelan, K., *Makers and Takers: The Economics of the Kalshi Prediction Market.* Circulates as CEPR DP 20631 (Sept 2025), CESifo WP 12122, UCD WP2025_19, MPRA 126350. Working paper, not yet journal-published — **check journal status immediately before our submission**; cite the January 2026 version.

---

## 0. What this is and why it works

BDW provide the first systematic evidence on Kalshi pricing (2021 → April 2025) and find a strong favorite–longshot bias with a maker/taker asymmetry. Two facts make a replication-plus-extension unusually valuable rather than routine:

1. **Their sample cutoff is a fee-regime boundary, not an arbitrary date.** They stop at April 2025 explicitly because "Kalshi began to charge fees on Makers after April 2025." Everything after their window therefore lives under a treatment that directly taxes the paper's headline exploitable margin (maker returns of +2.6% on contracts ≥50c). Our extension window is a natural experiment they could not run.
2. **They explicitly invite the persistence test** (Section 6): "We think it will be interesting to see if the biases and return patterns that we have reported persist now that they have been publicly documented." Publication (CEPR DP, 2025-09-08) and the maker-fee change (April 2025) are ~5 months apart — two distinct treatments whose timing separation permits a coarse decomposition.

Positioning: this is the prediction-market twin of the first paper's C2 frame (post-publication persistence, McLean & Pontiff 2016), on a different venue, a different anomaly, and — unlike paper #1 — on purely historical data, so it can be completed on its own schedule.

## 1. Pinned facts from the source paper (verify nothing here from memory — all from the PDF)

**Sample & construction.** Kalshi inception 2021 → April 2025. Transaction-level data via Kalshi's API (they registered for access). Filters: total traded volume at closure ≥ $1,000; final bid–ask spread ≤ 20c; market open ≥ 24 hours (excludes hourly reset crypto/index series); 63 Yes contracts dropped for mismatch vs Kalshi's separately-reported final prices. Result: 12,403 events, 46,282 Yes contracts. Prices: last trade on closing day plus last trade before the same time on each of up to 10 prior days → 156,986 Yes prices; descriptives double to 313,972 by adding the No side (regressions use Yes only — the No side is a linear transform and would fake precision). Two-thirds of all prices sit in the tails (106,209 in 1–10c; 106,209 in 90–99c, symmetric by construction).

**Headline quantities to reproduce (R1 targets).**
- Win-rate-vs-price curve (their Fig. 3): FLB pattern; low-price contracts win less than price implies, high-price more.
- Returns by 10c band (Fig. 5): contracts ≤10c lose >60%; small positive returns above 50c; statistically significant positive post-fee returns above 70c; average pre-fee return ≈ **−20%**.
- Maker/taker split (Fig. 6, Table 10): average return **Makers −9.64%, Takers −31.46%**; makers ≥50c earn **+2.6%**; maker share rises monotonically from 43.5% (1–10c) to 56.5% (90–99c).
- Mincer–Zarnowitz regression `(Y−P) = α + ψP + ε` in cents, Yes contracts only, SEs two-way clustered (event, Yes-contract): full sample **ψ = 0.034 (0.005), α = −1.736 (0.153)**, n = 156,986; F-test of α=ψ=0 rejects everywhere.
- By-year ψ (Table 9): 2021 **0.041***, 2022 **0.023**\*\*, 2023 **0.036***, 2024 **0.048***, 2025 (Jan–Apr) **0.021*** (10% level only) — their own "some evidence the bias is diminishing."
- MAE declines monotonically toward close (Fig. 4); bias holds across volume quintiles, transaction-size quintiles, and categories (ψ smaller/insignificant for politics and entertainment — Table 8).
- Fee model in their window: takers only, `$0.07·P(1−P)` per contract, total rounded up to the next cent; they impute the fee on a 100-contract purchase (≈1.77% at 50c). Makers: zero fee pre-May-2025.

**Their robustness notes we inherit:** results insensitive to cutting at Dec 2024 or excluding sports (sports launched early 2025 and volumes jumped in Jan 2025 — the 2025 subsample is confounded by both partial-year and category-mix shift; our monthly-resolution extension handles this better than their annual split).

## 2. Research questions

- **R1 (reproduction):** On independently fetched data for 2021→2025-04-30, applying their exact filters and construction, do we reproduce the headline quantities above? Success criterion (stated ex ante): same signs, same significance pattern, ψ and the maker/taker gap within ordinary sampling variation of their point estimates; quantitative deltas reported in full either way. Divergences are findings, not failures — likely sources: API data revisions, delisted series, our filter implementation.
- **R2 (extension — the contribution):** On 2025-05-01 → [2026-12-31], do the FLB and the maker/taker asymmetry persist under (a) maker fees and (b) publication? Primary estimands: monthly/quarterly ψ path (same MZ spec, same clustering); tail-bin (≤10c) post-fee loss rate over time; maker-vs-taker average-return gap over time; maker ≥50c return (the directly-taxed, directly-publicized margin) over time. Coarse decomposition via sub-windows: 2025-05→2025-08 (fees only), 2025-09→ (fees + publication). Honesty note in the paper: the two treatments are not randomized and other things changed (volume growth, category mix, the late-2025 fee revisions) — we report paths and timing, we do not claim clean causal separation.
- **R3 (prospective arm, optional, clearly fenced):** From 2026-07-04 the lab's own live Kalshi collection adds order-book depth and spreads at forecast time — covariates BDW's transaction data lacks. Small n; include only as a labeled supplement (e.g., do taker losses concentrate where books are thin?), never pooled with R1/R2.

## 3. Data plan

- **Source:** Kalshi public market-data API (`trade-api/v2`): settled markets via `/markets` (status filter, cursor pagination), transaction history via `/trades` per market (includes `taker_side` — the field that makes the maker/taker split possible without Lee–Ready-style inference). Candlestick/last-price endpoints as cross-check for the "63 mismatched contracts" filter analogue.
- **Step zero — access check (hard gate):** BDW *registered* for API access. Verify whether full historical trades are currently served without authentication. If any required endpoint needs an account/API key: **STOP and surface the decision** — creating a Kalshi data account is a ToS-acceptance decision for the operator, not for the implementer, and it sits outside the lab's guardrails because this is a separate repo; it still gets decided by a human, explicitly. Do not silently register.
- **Politeness:** ≤5 req/s sustained (documented ceiling ~10), exponential backoff on 429/5xx, resumable cursor state, one full pass then incremental. Rough volume: ~50–80k settled markets in scope era × (1 metadata page share + 1–N trade pages) — expect a 1–3 day polite fetch on the VPS; store raw JSON → Parquet partitioned by month + a SQLite index of markets/events.
- **Fee history table (its own small, sourced YAML, like the lab's):** pre-2025-05 taker `0.07·P(1−P)` rounded up (from the paper itself); **[VERIFY]** the actual maker-fee schedule effective May 2025 and any later revisions, from Kalshi's own published fee pages / changelog (web archive if needed) — the extension's post-fee returns are only as credible as this table; current schedule already sourced in the lab (effective 2026-07-07) can seed the latest row.
- **Event/series taxonomy:** map Kalshi `series`/`event`/`market` hierarchy to BDW's event/contract units; exclude hourly-reset series by series metadata (open duration <24h), mirroring their rule mechanically rather than by list.

## 4. Analysis discipline

- Separate repo, MIT, same engineering standards as the lab (tests on the scoring math, especially the fee rounding and the two-way clustering).
- **Commit `docs/analysis_plan.md` (this section, expanded) BEFORE computing any R2 estimate.** Honest framing: historical data means this is specification commitment, not outcome-blind pre-registration — say exactly that in the paper; R3 is the only genuinely prospective arm.
- Estimator parity: reproduce their MZ spec verbatim (cents units, Yes-only, two-way clustered SEs; event-level only where a contract appears once). Add — clearly labeled as *additional*, not replacement — an event-clustered bootstrap CI on the maker/taker return gap, reusing the lab's tested machinery.
- Decision vocabulary fixed ex ante (IJF replication-section style): **confirmed** (signs + significance pattern reproduce), **partially confirmed** (pattern reproduces, magnitudes materially different — quantify), **diverged** (sign or significance pattern breaks). R2 separately labeled: **persisted / attenuated / vanished / reversed**, each defined on the ψ path and the maker-margin path before looking.

## 5. Venue & escalation

- Default: **replication note** — IJF replication section (confirming replication ≈ short format) or IREE (dedicated replication journal).
- **Escalation rule (fixed now):** if R2 shows the maker ≥50c margin or the tail-bin loss rate materially regime-shifted after fees/publication, this stops being a note and becomes a short standalone paper ("Do prediction-market anomalies survive fees and publication? Evidence from Kalshi") — Economics Letters / Finance Research Letters class, and the McLean–Pontiff framing carries over from paper #1's Related Work almost verbatim.
- Either way: cite BDW's Section 6 invitation in the introduction; it does for this paper what Le's "designed to be re-estimated" quote does for paper #1.

## 6. Timeline (calendar-realistic, not optimistic)

| When | What |
|---|---|
| Days 1–3 | Access check (hard gate) → fetch job on VPS → raw archive frozen |
| Week 1–2 | R1 construction + reproduction tables; divergence log |
| Before any R2 number | `analysis_plan.md` committed |
| Weeks 3–4 | R2 paths + sub-windows; escalation decision per §5 |
| ~Mid-August 2026 | Full draft (note format), independent of paper #1's freeze |

## 7. Known risks, stated now

- **Historical API completeness:** Kalshi may not serve full 2021–2022 trade history publicly, or delisted series may be missing → R1 becomes a partial-window reproduction; report coverage explicitly against their 46,282-contract count.
- **Fee-history reconstruction** is the single most error-prone input (see §3); a wrong maker-fee schedule quietly corrupts every R2 post-fee return.
- **Scoop risk:** BDW themselves flagged the persistence question in print and have the data pipeline; a competing extension by the original authors is plausible. Mitigant: our marginal cost is days, not months — speed is the moat. Check SSRN/arXiv for BDW follow-ups before drafting.
- **Confounds in R2** (volume growth, sports mix, later fee revisions) — handled by disclosure and category-split robustness, not by overclaiming identification.

---

*Placeholder inventory: [2026-12-31] extension end (or earlier if drafting in August — then state the actual end date); [VERIFY] post-April-2025 maker-fee schedule with sources and dates; [VERIFY] public-access status of historical trades (step-zero gate); BDW journal status at submission time; R3 inclusion decision after seeing live-collection n in August.*
