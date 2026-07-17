# Agent DOOM VLM Benchmark

This is the offline path for testing real vision models against Agent DOOM perception artifacts before adding model weight to the capsule.

The capsule already writes triggered local artifacts:

- `/run/pairputer/vision_state.json`
- `/tmp/vision_events/*.jpg`
- `/tmp/vision_events/*.json`

Copy those artifacts before stopping the local capsule:

```bash
mkdir -p /tmp/agent-doom-vision-events
docker cp pairputer-local-agent-doom:/tmp/vision_events/. /tmp/agent-doom-vision-events/
```

## Candidate Models

The shortlist lives in `vlm_candidates.json`.

Print the current plan:

```bash
python3 capsules/agent-doom/vision_bench/download_vlm_models.py --dry-run --all
```

Optionally download one candidate into the gitignored local cache:

```bash
python3 capsules/agent-doom/vision_bench/download_vlm_models.py qwen2-vl-2b-q4-k-m
```

You can also skip downloads and let current `llama-server` pull by Hugging Face alias:

```bash
llama-server -hf bartowski/Qwen2-VL-2B-Instruct-GGUF:Q4_K_M --port 8080
```

Other useful aliases:

```bash
llama-server -hf ggml-org/moondream2-20250414-GGUF:F16 --port 8080
llama-server -hf ggml-org/SmolVLM-256M-Instruct-GGUF:Q8_0 --port 8080
llama-server -hf jc-builds/smolvlm2-500m-gguf:Q8_0 --port 8080
```

## Build A Labeled Corpus

Trigger names are not ground truth. A `no_kill_speedrun_damage` frame can show
damage without a visible enemy. Create an explicit label file, then freeze the
event dump into a local corpus:

```bash
python3 capsules/agent-doom/vision_bench/labeled_corpus.py template \
  --events-dir /tmp/agent-doom-vision-events \
  --out /tmp/agent-doom-vision-events/labels.json

python3 capsules/agent-doom/vision_bench/labeled_corpus.py init \
  --events-dir /tmp/agent-doom-vision-events \
  --labels /tmp/agent-doom-vision-events/labels.json \
  --out capsules/agent-doom/vision_bench/corpora/e1m1-speedrun-local \
  --name e1m1-speedrun-local

python3 capsules/agent-doom/vision_bench/labeled_corpus.py validate \
  --corpus capsules/agent-doom/vision_bench/corpora/e1m1-speedrun-local \
  --strict
```

`corpora/` is gitignored because it contains game screenshots. The committed
tooling and manifest format are the durable contract.

## Run The Benchmark

With `llama-server` running locally:

```bash
python3 capsules/agent-doom/vision_bench/benchmark_vlm.py \
  --events-dir /tmp/agent-doom-vision-events \
  --server-url http://127.0.0.1:8080 \
  --model qwen2-vl-2b-q4-k-m \
  --output /tmp/qwen2-vl-doom-vision-report.json \
  --jsonl /tmp/qwen2-vl-doom-vision-report.jsonl
```

For fair scoring, prefer a hand-labeled file over trigger-derived expectations:

```bash
python3 capsules/agent-doom/vision_bench/benchmark_vlm.py \
  --events-dir /tmp/agent-doom-vision-events \
  --labels /tmp/agent-doom-vision-events/labels.json
```

Or benchmark a frozen corpus directly:

```bash
python3 capsules/agent-doom/vision_bench/benchmark_vlm.py \
  --corpus capsules/agent-doom/vision_bench/corpora/e1m1-speedrun-local
```

## Train The Tiny Adapter

Small general VLMs have been fast but unreliable on 224px DOOM frames. The
adapter path trains a tiny deterministic exemplar model from labeled corpus
frames and runs in the capsule without a model server:

```bash
python3 capsules/agent-doom/vision_bench/train_vision_adapter.py train \
  --corpus capsules/agent-doom/vision_bench/corpora/e1m1-speedrun-local \
  --out capsules/agent-doom/rootfs/opt/capsule/vision_adapter_model.json \
  --name e1m1-speedrun-local-adapter

python3 capsules/agent-doom/vision_bench/train_vision_adapter.py eval \
  --corpus capsules/agent-doom/vision_bench/corpora/e1m1-speedrun-local \
  --model capsules/agent-doom/rootfs/opt/capsule/vision_adapter_model.json \
  --leave-one-out
```

Enable it in the capsule with:

```bash
PAIRPUTER_VISION_PROVIDER=adapter
```

`labels.json` is keyed by image filename or stem:

```json
{
  "1783211378527-no_kill_speedrun_damage-4.jpg": {
    "is_enemy_visible": true,
    "is_door_visible": false
  }
}
```

For tiny caption-first VLMs that see objects but do not obey JSON, run an explicitly separate adapter score:

```bash
python3 capsules/agent-doom/vision_bench/benchmark_vlm.py \
  --events-dir /tmp/agent-doom-vision-events \
  --labels /tmp/agent-doom-vision-events/labels.json \
  --prompt-mode caption \
  --allow-caption-fallback
```

For a single frame:

```bash
python3 capsules/agent-doom/vision_bench/benchmark_vlm.py \
  --image /tmp/agent-doom-vision-events/1783211378527-no_kill_speedrun_damage-4.jpg
```

## Pass Criteria

The harness grades:

- JSON validity: model must return parseable JSON matching the expected schema.
- Trigger-specific accuracy:
  - `no_kill_speedrun_damage` must set `is_enemy_visible: true`.
  - `repeated_failed_use` must identify a door, switch, or blocking wall.
  - `route_open_but_blocked` must identify a door, wall, or exit affordance.
  - `exit_target_ambiguous` must identify an exit, switch, or door.
- Speed:
  - `target_ms` defaults to `2500`.
  - `discard_ms` defaults to `5000`.
  - Anything over `5000ms` locally should not be baked into the capsule.

The benchmark exits:

- `0` if every frame is valid JSON, passes trigger accuracy, and stays under discard latency.
- `1` if no input images were found.
- `2` if at least one model response failed JSON, accuracy, or discard latency.

## Wiring The Winner Later

Do not bake model weights into the capsule Docker image until a model passes this offline gate. Once a candidate wins, the capsule already has a non-default `llama_server` provider path:

```bash
PAIRPUTER_VISION_PROVIDER=llama_server
PAIRPUTER_VISION_SERVER_URL=http://127.0.0.1:8080
PAIRPUTER_VISION_MODEL=qwen2-vl-2b-q4-k-m
```

Caption-first models can opt into the same adapter path:

```bash
PAIRPUTER_VISION_PROMPT_MODE=caption
PAIRPUTER_VISION_ALLOW_CAPTION_FALLBACK=1
```

Then:

1. Start `llama-server` from `start.sh` with the winning model and projector.
2. Keep `FakeVisionProvider` as the default for small images/dev loops.
3. Keep `/brain/vision_status` compact; never return images or raw model text through MCP.
4. Re-run the autonomy gates and compare latency/trigger counts before baking any `.gguf`.
