# Getting files in and out of the Workbench VM

The Workbench MicroVM is **isolated** — it shares NO filesystem with the host (no bind
mounts, no drag-and-drop, no shared clipboard with the host machine). Files cross the
boundary only through the capsule's own workspace tools, which are authenticated,
confined to the workspace root, and integrity-verified. That isolation is a feature, not
a gap: bytes enter through a hash-checked tool path, never an ambient shared folder.

## The three ways to put a file in the VM

| Goal | Tool | Notes |
|---|---|---|
| Author content you can generate | `workspace_write` | Fastest. You supply `content`; it's written atomically with SHA evidence. |
| Transfer a file's raw bytes (e.g. from the host) | `workspace_upload` | Chunked base64, per-chunk + whole-file SHA verification, atomic commit. ≤ 8 MiB. |
| Make a directory | `workspace_mkdir` | Rarely needed now — write/upload auto-create parents. |

`workspace_read` / `workspace_describe` / `workspace_list` read back out.

## "Just works" behavior (baked into the tools)

Two things that used to require extra ceremony are now automatic — the tool descriptions
say so, and the code enforces it (`services/workspace_service.py`):

1. **Auto-commit on completion.** `workspace_upload` commits as soon as the staged bytes
   reach `total_size`. You do **not** need to pass `final: true` (it still works and forces
   the completeness check). A short chunk that doesn't reach `total_size` stays staged, so
   multi-chunk uploads are unaffected.
2. **Auto-create parent directories.** Both `workspace_write` and `workspace_upload`
   create missing parent dirs in `path` (confined, `O_NOFOLLOW`, mount-escape checked) —
   no separate `workspace_mkdir` first. Writing `from-host/report.txt` into an empty
   workspace just works.

## The one thing to always do: fresh revision per call

Every workspace mutation carries `expected_world_revision`. Any intervening effect
(another write, a mkdir, human input) **advances the world revision**, and a call with a
stale revision is rejected (`world_revision_changed`). So **fetch a fresh
`expected_world_revision` from `:6906` (or the last receipt) immediately before each
mutating call** — don't reuse one captured several calls ago. This is the anti-drift guard
that keeps two actors from clobbering each other; it is working as intended.

## Worked example — transfer a host file into the VM

```python
import json, base64, hashlib, urllib.request
KEY = open("/run/pairputer/bridge-ingress.key").read().strip()   # from inside the VM/bridge
data = open("some-file.png", "rb").read()
total_sha = hashlib.sha256(data).hexdigest()

def post(body):
    req = urllib.request.Request(
        "http://127.0.0.1:6905/workspace/upload",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "X-Pairputer-Bridge-Capability": KEY})
    return json.load(urllib.request.urlopen(req, timeout=15))

# fetch a FRESH revision right before uploading
state = json.load(urllib.request.urlopen("http://127.0.0.1:6906/"))
epoch, rev = state["humanEpoch"], state["worldRevision"]

# ≤ ~512 KiB raw per chunk; here it fits in one. No final=true, no mkdir needed.
CHUNK = 400_000
off = 0
while off < len(data):
    piece = data[off:off+CHUNK]
    post({
        "path": "from-host/some-file.png", "upload_id": "xfer-1",
        "offset": off, "chunk_base64": base64.b64encode(piece).decode(),
        "chunk_sha256": hashlib.sha256(piece).hexdigest(),
        "total_size": len(data), "total_sha256": total_sha,
        "action_id": f"u{off}", "idempotency_key": f"uk{off}",
        "expected_human_epoch": epoch, "expected_world_revision": rev,
    })
    off += len(piece)
# committed to /home/app/workspace/from-host/some-file.png, sha == total_sha
```

Verify inside the VM: `sha256sum /home/app/workspace/from-host/some-file.png` matches
`total_sha` — or without a terminal, `workspace_describe` (via `capsule_invoke` on the deployed
substrate; it is not in the advertised tool list) returns the stored file's sha256.

**Proven live, three times:**
- Local container: a 111 KB host file transferred byte-identical, hash matched.
- **AWS production VM (2026-07-11)**: a 293 KB PNG in 3 × 128 KiB chunks, 6.9 s wall, VM-side sha256
  identical to the host's. Fetch a FRESH `expected_world_revision`/`expected_human_epoch` (via
  `observe`) before EACH chunk — an intervening mutation advances the revision and a stale one is
  rejected.
- **Driven by a real host model (2026-07-11)**: Codex (gpt-5.5) composed the full `workspace_upload`
  envelope from the tool schema alone — fresh epoch/revision, chunk + total shas, idempotency key —
  and verified the result through `capsule_metadata` → `capsule_invoke workspace_describe`. No
  terminal, no shared filesystem, cryptographically verified end-to-end.

## What does NOT work (by design)

- **No shared drive / drag-and-drop** between host and VM — isolated MicroVM.
- **No host↔VM clipboard bridge** — "copy on the host, paste into a VM app" won't work
  (separate machines, separate clipboards). Copying *within* the VM is fine.
- **Opening a transferred file** is a separate step: once bytes are in the workspace,
  launch the right app (`open_app` for the image viewer / editor, or `run_command` to
  invoke it) — the upload only lands the file, it doesn't open it.
