# Kalshi Makers & Takers Replication

Replication and extension of Bürgi, Deng & Whelan, *Makers and Takers: The Economics of the Kalshi Prediction Market* (CEPR DP 20631). Read-only research instrument — no execution code, no order placement, no Kalshi account required by default. See [`Claude.md`](Claude.md) for the full implementation brief (single source of truth for scope, methodology, and phasing); [`kalshi-replication-spec.md`](kalshi-replication-spec.md) is a superseded v1.0 draft, kept for history only.

This is a separate, standalone repository — not part of, and not dependent on, `polymarket-forecast-lab`.

## Quickstart

```
uv sync
uv run pytest
uv run kmt --help
uv run kmt step-zero   # the hard gate: verifies Kalshi's public API has what this replication needs, before any fetch
```

`kmt step-zero` is a hard gate (spec §3): if any required Kalshi endpoint turns out to need authentication, it exits with a STOP banner and instructions, and registers nothing on its own. Read `reports/step_zero/findings.md` after it runs.

## Status

Phase 0 (scaffold) + Phase 1 (Step Zero) built. Everything after Step Zero — the two-pass fetch pipeline, R1 reproduction, fee schedule, R2 extension, Polymarket control-venue overlay, R3 prospective arm — is specified in `Claude.md` and not yet built.
