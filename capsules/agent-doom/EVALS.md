# Agent DOOM evals

The eval taxonomy for the agent-doom capsule. Five tiers plus a vision benchmark, from fast unit
tests that need no capsule up to Codex-in-the-loop acceptance. See [`README.md`](./README.md) for the
capsule architecture the evals exercise.

## Tiers

| Tier | What it proves | Command | Needs capsule? | Runtime |
|---|---|---|---|---|
| Unit / behavioral | The combat FSM, goal-contract parse, planner, and eval-runner scoring are correct in isolation. `test_agent_doom_goal_contract.py` alone pins ~222 cases of the combat FSM + goal-contract semantics. | `python3 -m pytest tests/ -q` (from repo root) | No | seconds |
| Goal-contract fuzz | Free-form goal phrasings compile to the expected objective + constraints (natural-language robustness of `goal_contract.py`). | `python3 capsules/agent-doom/goal_fuzz.py` (corpus: `eval-goals/free_form_goal_fuzz.json`) | No | seconds |
| Live behavior harness | The running bridge on `:6905` produces real game-state deltas — drives it like the LLM would and asserts actual seconds/kills/health changes, not mocks. | `python3 capsules/agent-doom/test_harness.py` | Yes (local docker) | ~1 min |
| Hard live gates | Autonomy benchmark: first kill under time, melee no-ammo kill, no-kill exit, level completion, clear-room multi-kill, compact map status. 13 checks; any failure fails the suite. | `python3 capsules/agent-doom/eval_gates.py --bridge http://127.0.0.1:6905` | Yes (local bridge) | ~2-3 min |
| Product matrix (scoreboard) | The end-to-end scoreboard over the default case matrix, with reliability scoring (`--repeat`), failure clustering, and per-case rate gates. The verdict (exit code) comes from the gates. | `python3 capsules/agent-doom/eval_runner.py` (add `--repeat N`) | Yes (local bridge, or MCP/tmux driver) | minutes to hours |
| Vision benchmark | The triggered vision sidecar (`vision_*`) against a labeled screenshot corpus. | see `vision_bench/README.md` | model-dependent | varies |

The product matrix runs in three **modes** (`--mode`): `direct` (fast local, talks straight to the
bridge — the default), `mcp-command` (wraps a real external MCP client command), and `tmux-codex`
(drives a tmux pane running Codex/Claude for final Codex-in-the-loop acceptance, parsing a marked
result out of the pane). The default case matrix is `DEFAULT_CASES` in `eval_runner.py`:
`e1m1-beat-level`, `e1m1-no-kill-exit`, `e1m1-punch-enemy`, `e1m1-clear-room`, and `human-interrupt`.
Extra case files live under [`eval-cases/`](./eval-cases/) and load via `--cases`.

## Probabilistic-case design

Most cases are **deterministic**: the same seed produces the same run, so every run must pass. But
DOOM's hitscan enemies (zombieman/shotgunner) roll damage per shot, so a single run of a
damage-sensitive case is a coin flip — passing or failing on the RNG, not on the agent's behavior.
Judging those on a single run would make the scoreboard flap.

The runner handles this with a per-case `min_success_rate`:

- `min_success_rate = 1.0` (the default) is a **deterministic** case: it keeps per-run semantics —
  every run must pass or the case fails.
- `min_success_rate < 1.0` is a **probabilistic** case: it is judged by its observed **pass rate over
  repeats**, not per-run. It needs **≥ 5 runs to gate** (fewer runs report `insufficient_runs` and
  don't affect the verdict). Gate `pass` iff `rate >= min_success_rate`.

The suite exit code keys off these gates: a deterministic case still fails on any single failed run;
a probabilistic case fails only when its measured rate drops below its floor. Use ≥ 5 runs to gate
(regression detection); use ≥ 30 runs to **adjudicate** a build change or re-tune a floor — see
[`eval-baselines/`](./eval-baselines/).

**Pre-state your decision rule before running an adjudication batch** (e.g. "≥ 25% → keep,
≤ 15% → revert") — at a true rate of ~30%, 10-run batches range from 0/10 to 4/10 on noise alone, and
reading the result after the fact invites motivated interpretation. The `avoid_damage` semantic is
"survive one median bullet, two is a fail": zombieman damage quantizes to `3×d5 ∈ {3,6,9,12,15}`, and
the health allowance is `PRESERVE_HEALTH_DAMAGE_ALLOWANCE = 9` (in `contract_eval.py`, mirrored in
`eval_runner.py`; `tests/test_health_allowance_sync.py` keeps the two in sync).

### Baselines

Definitive multi-run baselines, committed under [`eval-baselines/`](./eval-baselines/):

| Case | Date | Commit | Runs | Pass rate | Gate | Notes |
|---|---|---|---|---|---|---|
| `e1m1-clear-room` | 2026-07-08 | `e1628a3` | 30 | 9/30 = 30% | `min_success_rate=0.15` | skill 3, seed 0, 2400-tic budget, `avoid_damage` allowance 9hp. 19 damage-budget violations, 2 stalls, 9 clean kills. Floor 0.15 flags a true regression to ~10% while tolerating noise. |

The allowance ladder measured on a prior 30-run distribution: `≤5hp → 20%`, `≤9hp → 30%` live. The
censoring effect — runs that survive a single graze keep fighting and often finish clean — is why the
live rate at allowance 9 sits at 30% rather than lower.

## Adding a case

Cases are `EvalCase` records (`eval_runner.py`), loadable from a JSON file via `--cases` (a JSON
array, or an object with a `cases` key). Fields:

| Field | Meaning |
|---|---|
| `id` / `case_id` | Stable case identifier (≤ 80 chars). |
| `goal` | The free-form goal string handed to the agent. |
| `objective` | Optional compiled objective (e.g. `complete_level`, `clear_area`, `kill_enemy`). |
| `constraints` | List of constraint tokens (e.g. `no_kills`, `no_ammo`, `fist_only`, `avoid_damage`). |
| `max_tics` | Tic budget for the run. |
| `reset_episode` | Reset to a clean episode start before the run. |
| `episode` / `map` / `skill` / `seed` | Level + difficulty + RNG seed selection. |
| `human_interrupt_after_s` | If > 0, inject a human-interrupt after N seconds (handoff cases). |
| `tags` | Free-form tags for clustering/filtering. |
| `min_success_rate` | `1.0` (default) = deterministic, every run must pass. `< 1.0` = probabilistic, gate on rate over ≥ 5 runs. |

For a new probabilistic case: measure a ≥ 30-run baseline first, promote it to `eval-baselines/`, set
`min_success_rate` below the measured rate with margin for binomial noise (the clear-room case is
gated at 0.15 against a measured 0.30), and run it with `--repeat ≥ 10`.
