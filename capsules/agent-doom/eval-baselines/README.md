# eval-baselines

Committed, adjudication-grade baseline runs for probabilistic eval cases. These are the raw
`--repeat` scoreboard rows (`eval_runner.py` JSONL) captured at a known commit, kept in git so a
later regression can be compared against the exact distribution the rate gate was tuned to. Ad-hoc
runs stay under the gitignored `eval-results/`; only a definitive baseline is promoted here.

See [`../EVALS.md`](../EVALS.md) for the probabilistic-case design and how the rate gate is derived
from these numbers.

| File | Case | Date | Commit | Runs | Result | Notes |
|---|---|---|---|---|---|---|
| `2026-07-08-e1m1-clear-room-30runs.jsonl` | `e1m1-clear-room` | 2026-07-08 | `e1628a3` | 30 | 9/30 = 30% pass | skill 3, seed 0, 2400-tic budget, `avoid_damage` allowance 9hp; 19 damage-budget violations, 2 stalls (`max_tics_exceeded`), 9 clean kills. Gate set at `min_success_rate=0.15`. |

Notes on the clear-room baseline:

- `status: "success"` counts as a pass; the pass rate is over the 30 rows, not per-run.
- Rows are stamped `commit: e1628a3` (the run's recorded HEAD). `e1628a3` is the direct parent of
  `a80751d` ("First-contact approach…"); the combat behavior under test is unchanged between them.
- Successful runs land at `health_delta` in `{0, -5, -6, +1}` — inside the 9hp allowance. The
  censoring effect (a run that survives one graze keeps fighting and often finishes clean) is why the
  live rate at allowance 9 is higher than a naive "never take a hit" reading would predict.
