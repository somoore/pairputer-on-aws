# agent-doom capsule

The reference capsule for pairputer: DOOM running inside a container / Lambda MicroVM, driven by an LLM
agent over MCP. It is the worked example every other capsule is measured against — the place where the
agent bridge, the goal-driven brain, and the spatial planner are exercised end to end.

## What it is

RESTful-DOOM (`somoore/restful-doom`, Chocolate Doom + an in-process gRPC `DoomAgent`) runs on an Xvnc
display inside the capsule. On top of the engine sit three things the agent needs:

- an **agent bridge** on `:6905` — an HTTP/1.1 JSON shim over the in-process gRPC contract, the only
  agent-facing surface (reached in production through the MicroVM's authenticated `:443` proxy gateway);
- a **brain runtime** — a bounded, goal-driven FSM/drive loop that turns a natural-language goal into
  bounded tactical action;
- a **spatial planner** — sector/portal routing with threat pricing over the parsed WAD map.

Media and human co-play run over their own websockets: **audio `:6902`**, **video `:6903` (H.264)**,
**input `:6904` (XTEST)**, plus **VNC on `:6901`** (Xvnc `:5901` fronted by websockify/noVNC). The
human plays through VNC/input; the agent observes by default and acts only when invited (`coplay` state
on `:6906`).

## Module map

All capsule modules live under `rootfs/opt/capsule/`.

| Module | Role |
|---|---|
| `agent_bridge.py` | The agent-facing HTTP surface on `:6905` — routes `/observe`, `/brain/drive_goal`, `/reset_episode`, `/brain/map_status`, etc. onto the in-process gRPC `DoomAgent`. |
| `brain_runtime.py` | The bounded objective driver: goal-driven FSM / drive loop. Holds the combat behavior and the `PRESERVE_HEALTH_DAMAGE_ALLOWANCE` health budget. |
| `planner.py` | Capsule-local spatial planner — sector/portal routing with threat-priced sector routes over the parsed map. |
| `goal_contract.py` | Free-form goal compiler: natural-language goal → objective + constraint tokens. |
| `combat_state.py` | Small combat state machine (the tactical phases the brain drives through). |
| `door_memory.py` | In-memory door / use-line outcome tracker (avoids re-poking dead use-lines). |
| `world_memory.py` | Compact world-state memory for the brain. |
| `threat_model.py` | Deterministic enemy threat classification; feeds the planner's threat pricing. |
| `map_cache.py` / `wad_map.py` | Cached access to RESTful-DOOM map snapshots; minimal Doom-format WAD map reader for planning. |
| `probe_runtime.py` | Compact batched probe facade the brain reads game state through. |
| `trace_logger.py` | Optional deterministic trace logging. |
| `vision_adapter.py` / `vision_brain.py` / `vision_state.py` / `frame_sampler.py` | Optional triggered-vision path — a vision sidecar fired on ambiguity events, with a compact local state contract and X11 frame capture. |
| `input_ws.py` / `audio_ws.py` / `video_ws.py` | The `:6904` / `:6902` / `:6903` media websockets. |
| `focus.py` / `input_selftest.py` | X focus helper; build-time XTEST input self-test (gates a broken-input build). |

## Local dev loop

Start the capsule locally (docker, no Lambda MicroVM) with the bridge on `:6905`:

```bash
substrate/local-dev.sh --capsule-only   # just the capsule; skip the MCP server
```

This runs the container as `pairputer-local-agent-doom` with ports `6901`-`6906` + `9000` mapped. To
iterate on a single capsule module without a full rebuild, copy it into the running container and
restart:

```bash
docker cp rootfs/opt/capsule/brain_runtime.py pairputer-local-agent-doom:/opt/capsule/brain_runtime.py
docker restart pairputer-local-agent-doom
```

Poke the bridge directly:

```bash
curl -s -X POST http://127.0.0.1:6905/observe -d '{}'
```

## Evals

The eval taxonomy — unit/behavioral tests, goal-contract fuzz, the live behavior harness, the hard
live gates, the product-matrix scoreboard, and the vision benchmark — is documented in
[`EVALS.md`](./EVALS.md). Committed multi-run baselines live in [`eval-baselines/`](./eval-baselines/).
