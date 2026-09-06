# ChatGPT CSP-on and remount retest

**Status: live retest outstanding.** Code review and deterministic regressions were run on
2026-09-06 against base `b68a1bf3c3fea31541d89d63b6d41a5a7b6be2e5` plus this change.
Neither PROBE-3 nor PROBE-8 has new live ChatGPT evidence. Do not mark them passed from HTTP 200,
an open SSE connection, a local test, or a successful CSP-off session.

## What changed before the live retest

- Mute preference now survives host remounts through `window.openai.widgetState`.
- Lifecycle intent and the consumed open nonce survive a remount when localStorage is blocked or
  empty. A replayed frozen/stopped card stays inactive; a genuinely new explicit open can run.
  Existing shared storage takes precedence over a stale widget snapshot, and another capsule's
  snapshot is ignored. Snapshot fields contain no relay credentials.
- Both the relay iframe player and the direct player discard decoded audio while their playback
  context is suspended. This avoids scheduling an unbounded backlog against a stopped audio clock.
  Late output from a replaced decoder is also discarded. This does not grant autoplay permission.
- An empty display-mode response uses the observed OpenAI host mode (or inline), rather than
  assuming the requested mode was granted. State is saved before the request can remount the card.
- The retest now targets Fullscreen / Inline. The widget PiP button was removed on 2026-07-12.

The resource URI/MIME and relay allowlists are unchanged. The server emits both `_meta.ui.csp`
and the legacy `openai/widgetCSP` fields. Their roles and host state persistence are documented in
the [OpenAI UI reference](https://developers.openai.com/plugins/reference).

## Highest-value checks, in order

| Priority | Check | Required evidence and pass criterion |
|---|---|---|
| P0 | Establish CSP-on provenance | Record host/client version, widget build commit, relay image digest, capsule release, and actual CSP toggle state. Refresh the connector and open a fresh card. Inspect the effective widget policy and iframe permissions. Allowed relay frame/streams work. A disposable unlisted-origin negative control is blocked by the expected CSP directive before any request leaves. |
| P0 | Frozen/stopped remount safety | Freeze, leave/reopen the thread, then remount through a display transition where available. Repeat after a deliberate stop on a disposable session. Stale RUNNING tool output must not launch/thaw, open streams, or restart billing. Verify control-plane state and absence of `ensure_running=true`. An explicit Thaw/Launch must still work. |
| P1 / PROBE-3 | Audio/autoplay | Run the cold-start, user-gesture, mute, interruption, and recovery cases below. Demonstrate nonzero decoded audio and actual audible playback after the permitted gesture. No stale burst after an autoplay block; mute remains effective. |
| P1 / PROBE-8 | Fullscreen and actual remount | Inline → Fullscreen → Inline with running video/input/audio. Record whether the document actually remounted, granted mode, safe-area layout, session identity, mute state, and stream recovery time. No duplicate live players or unintended wake. |
| P1 | Token renewal and reconnect | Stay active beyond the configured token lifetime (default 15 minutes), including an audio-enabled mode transition. Capture renewal and resumed decoded video/audio. Old streams close and fresh signed URLs recover. Repeat Freeze during recovery; it must win. |
| P2 | Host regression | Repeat the relevant rows on web, desktop and the supported mobile client, plus a separate Codex smoke test. Record unavailable capabilities as N/A for that host. A shared connector is not equivalent host evidence. |

Start with web on an already-owned disposable deployment. Record the deployed widget and relay
versions separately: testing new widget code against an old relay would miss half of the audio fix.
No stack deployment is required to run the local checks below.

## PROBE-3: audio/autoplay procedure

1. Use a known, short non-silent sound in the guest. On a fresh card, observe the audio path before
   any gesture: SSE packets, AudioDecoder output, AudioContext state, gain/mute state and audible
   output. Record codec/browser support. Successful transport alone is not an audio pass.
2. Exercise the Sound/Muted control. Its initial `Sound` label describes the unmuted preference,
   not proof of playback; the first click currently mutes. Return it to Sound and observe whether
   playback starts. If still suspended, click inside the player and record that extra gesture.
3. Distinguish CSP denial, iframe Permissions Policy/autoplay denial, unsupported codecs, decode
   errors, and silent guest output. The iframe's `allow="autoplay; fullscreen"` does not by itself
   prove permission through the host's entire ancestor chain. Do not widen CSP to fix autoplay.
4. Remain autoplay-blocked long enough to receive multiple seconds of sound, then unlock playback.
   New sound must play without a burst of queued old samples. Mute, remount, and verify it stays muted.
5. Unmute, Freeze/Thaw, remount, and renew the session token. Confirm sound recovers or expose and
   document the precise required gesture. Check video and input concurrently. Repeat after a host
   background/foreground interruption on supported clients.

## PROBE-8: display-mode/remount procedure

1. Record the session identity and mount identity using host/devtools observations. A CSS resize
   alone does not establish remount behavior. Boot must not request a display mode.
2. Click Fullscreen, observe the granted/actual mode and button label, then return Inline. If the
   SDK declines, test the native fallback where allowed; otherwise the widget must show a truthful
   unavailable message. Do not require a PiP button that no longer exists.
3. Verify new decoded frames, working pointer/keyboard coordinates, audio, and no duplicate streams
   after each transition. Record the measured reconnect time; the old July watchdog observation
   is not a new recovery measurement.
4. Repeat while muted, frozen, and stopped. Replay the original tool result, including its original
   open nonce. With localStorage blocked or absent, the host snapshot must preserve intent and mute.
   Then use a fresh explicit open and verify it can resume correctly.
5. Test a second capsule to detect accidental cross-restoration. Verify that host state and captured
   diagnostics contain no token, signed URL, CloudFront signature, or OAuth credential.

## Executable checks and evidence

Requirements: Python with pytest; Node.js on PATH (the existing host tests already execute Node).
No AWS credentials or network are used by these tests.

```bash
node --test tests/widget_runtime.cjs
python -m pytest tests/test_widget_runtime.py tests/test_hosts.py \
  tests/test_relay_session_freshness.py tests/test_audio_stream_limit.py -q
python -m pytest tests/ -q
```

`widget_runtime.cjs` executes the production widget script and player code with deterministic
host/DOM/transport/audio fakes. It exercises remount boot and control handlers, not just source
string presence. `test_widget_runtime.py` includes it in the normal suite and evaluates the real
resource metadata function for configured and absent relay origins.

| Evidence on 2026-09-06 | Result |
|---|---|
| New JavaScript regression cases | 13 passed. Before fixes, the initial harness reproduced mute loss, frozen/stopped replay starting streams, unavailable-storage recovery failure, and audio buffering in both engines. |
| Focused Python test selection above | 49 passed, including the JS suite wrapper and CSP metadata contract. |
| Full Python suite | 986 passed; 4 process-launch tests failed with `SubprocessError: Exception occurred in preexec_fn`. The same four fail on untouched base `b68a1bf` in this environment. |
| Rendered local browser check | Blocked before navigation: `net::ERR_BLOCKED_BY_CLIENT` for the local fixture URL. No rendered behavior or browser policy pass claimed. |
| Live ChatGPT CSP-on / PROBE-3 / PROBE-8 | Not run; pending. |

The four baseline failures are `test_process_job_is_tracked_and_bounded`,
`test_process_output_is_redacted`, `test_process_shell_ignores_profiles_and_rejects_home_override`,
and `test_coding_job_and_opt_in_localhost_preview_share_one_broker` in
`tests/test_computer_use_runtime.py`. No process sandbox controls were changed to make tests pass.

## Record each live result

For each matrix row record: date, host/browser/OS versions, widget commit, relay digest, capsule
release, CSP toggle and effective policy, initial state, action, expected result, observed result,
PASS/FAIL/BLOCKED/N/A, sanitized evidence location, and follow-up fix/test. Keep raw signed request
URLs and tokens out of committed logs and screenshots.

Close PROBE-3 only with live CSP-on audio evidence. Close PROBE-8 only with current display-mode
and actual remount evidence, including inactive lifecycle safety. Otherwise leave the row pending
and state exactly which observation is missing.
