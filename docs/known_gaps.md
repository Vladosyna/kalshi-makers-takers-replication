# Known gap: live-sweep sub-window 1769072393-1782863999

**Status as of 2026-07-18:** worked around, not fully collected. Needs
follow-up before this range is treated as complete for R1/R2 discovery.

## What happened

Under the normal `discover_live_window` 8-way split (`n_concurrent_windows=8`,
the default `run_pass1` uses), the sub-window `(1769072393, 1782863999)`
(2026-01-22 .. 2026-06-30, the densest, sports-heavy tail near `R2_END`)
got stuck on one specific pagination cursor:

```
cursor=CgYI5cbu0AYSOUtYTVZFU1BPUlRTTVVMVElHQU1FRVhURU5ERUQtUzIwMjZCMTQzQzVFRTJFMS01Q0Y3MDVGRDJFQw
min_close_ts=1769072393&max_close_ts=1782863999
```

Verified independently via `curl` (not just our client) that Kalshi's own
server consistently 504s on this exact cursor after ~10.4s, at every page
`limit` tried (1000/100/10), while the same date range's page 1 (no cursor)
responds in <0.5s. This is a server-side issue with this one pagination
position, not our code, our network, or a request-size problem.

Three consecutive `kmt fetch pass1` restarts (2026-07-18, ~00:36-00:42 UTC)
all crashed identically on this cursor after exhausting retries -- the
whole collector process dies on any single request's exhausted retries,
not just that one sub-window (a real resilience gap in
`discover_live_window`, not yet fixed).

## Workaround applied

Re-split the SAME date range `(1769072393, 1782863999)` into 40 finer
sub-windows (`discover_live_window(start_ts=1769072393, end_ts=1782863999,
n_concurrent_windows=40)`), on the theory that the stuck cursor sits at one
narrow internal offset and a finer split would isolate it to a small slice
instead of blocking the whole 160-day range. These 40 sub-windows are
checkpointed under their OWN `(window_start, window_end)` keys in
`live_window_scan_state` (span ~5,000,000s / ~58 days... actually ~4 days
each, span=(1782863999-1769072393)/40 ≈ 344,700s ≈ 4 days) -- separate from,
and not conflicting with, the original 8-way keys.

**Result as of when this was stopped (2026-07-18):**

- **29 of 40** fine-grained sub-windows reached `done`.
- **11 of 40** remain `in_progress`, checkpointed with real progress
  (9 to 346 pages fetched each) and a resumable cursor -- calling
  `discover_live_window(start_ts=1769072393, end_ts=1782863999,
  n_concurrent_windows=40)` again will resume each from where it left off,
  not from page 1.
- One of the 11, `(1778381723, 1778726513)` (2026-01-22 06:35 .. 2026-01-26
  05:41 UTC), appears to have hit the SAME kind of stuck-cursor issue as the
  original -- its checkpoint hasn't advanced past 189 pages since
  2026-07-17T23:53:20Z. This narrow ~4-day slice may need its own further
  split, or a later retry once whatever is wrong on Kalshi's side clears.
- The other 10 in-progress ones were actively advancing (not stuck) when
  this run was stopped to let the rest of the pipeline (historical scan,
  category resolution, panel/quote fetch) proceed -- this whole date range
  turned out to be extremely dense (hundreds of thousands of thin markets
  per ~4-day slice), so fully draining it will take a while regardless of
  the stuck-cursor issue.

## What still needs to happen

1. **Re-run the fine-grained walk to finish the 11 remaining slices**:
   ```python
   await discover_live_window(client, conn, start_ts=1769072393, end_ts=1782863999, n_concurrent_windows=40)
   ```
   Safe to re-run repeatedly (each call resumes from checkpointed cursors,
   skips the 29 already-`done` slices). Use bounded concurrency (a
   semaphore of ~3, not all-at-once) when driving this manually -- firing
   all 11 remaining lanes at once reliably triggers 429s.
2. If `(1778381723, 1778726513)` is still stuck when revisited, split it
   further (e.g. into 4-8 sub-windows of ~12-24h each) the same way.
3. Once all 40 (or their further splits) reach `done`, this file's gap is
   closed -- delete it or mark it resolved, and the coverage for this date
   range is then complete and equivalent to what the normal 8-way path
   would have produced.

## Why the main collector can proceed in the meantime

The ORIGINAL 8-way checkpoint entry for `(1769072393, 1782863999)` was
manually marked `done` in `live_window_scan_state` (its own true status was
"most, not all, of this range fetched via the fine-grained workaround
above") so that `kmt fetch pass1`'s normal `discover_live_window(... ,
n_concurrent_windows=8)` call stops retrying the exact stuck cursor and the
pipeline can move on to `discover_historical_series` /
`resolve_series_and_category` / panel+quote fetch, per the spec's own
phase ordering. This is a deliberate, documented white lie in one
checkpoint row, not a silent one -- this file is the record of it, and the
count-reconciliation gate in `r1/reconcile.py` (spec S1) will surface any
resulting under-count against BDW's pinned integers when R1's construction
actually runs, at which point this file explains why.
