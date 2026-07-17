import os

from .common import action_result, evidence, require_action_envelope
from .control_state import LeaseRejected


class ScreenshotService:
    def __init__(self, control, observer):
        self.control, self.observer = control, observer

    def capture(self, request):
        autonomy_env = os.environ.get("PAIRPUTER_WORKBENCH_AUTONOMY", "").lower() in {"1", "true", "yes", "on"}
        if autonomy_env:
            # Autonomy: a host can call screenshot BARE (no envelope). Fill sane defaults for any
            # MISSING OR EMPTY envelope field (the protobuf path yields "" / 0, not absent keys) so the
            # read-only capture just works; the response still reports the real epoch/revision.
            request = dict(request)
            if not request.get("action_id"):
                request["action_id"] = "screenshot"
            if not request.get("idempotency_key"):
                import time as _t
                request["idempotency_key"] = f"screenshot-{_t.time_ns()}"
        action_id, epoch, revision, _ = require_action_envelope(request)
        # A screenshot is READ-ONLY. A host's computer-use loop just wants to SEE the current frame;
        # forcing it to first observe() to learn the exact epoch/revision (and re-supply them) is pure
        # friction. In autonomy mode we capture the CURRENT frame and report the real epoch/revision
        # back — no pre-known values required. (Strict mode still binds to the supplied frame so the
        # targetProof stays anti-drift for the semantic exact-consent click path.)
        autonomy = os.environ.get("PAIRPUTER_WORKBENCH_AUTONOMY", "").lower() in {"1", "true", "yes", "on"}
        # Do not hold the preemption lock around ffmpeg.
        state = self.control.snapshot()
        if not autonomy and (epoch != state["humanEpoch"] or revision != state["worldRevision"]):
            reason = "human_epoch_changed" if epoch != state["humanEpoch"] else "world_revision_changed"
            return action_result(accepted=False, action_id=action_id, state=state,
                                 actuator="screen.x11grab", summary="screenshot rejected", reason=reason)
        result = self.observer.capture(request.get("x", 0), request.get("y", 0),
                                       request.get("width"), request.get("height"))
        current = self.control.snapshot()
        if not autonomy and (epoch != current["humanEpoch"] or revision != current["worldRevision"]):
            self.observer.discard(result)
            reason = "human_epoch_changed" if epoch != current["humanEpoch"] else "world_revision_changed"
            return action_result(accepted=False, action_id=action_id, state=current,
                                 actuator="screen.x11grab", summary="screenshot rejected", reason=reason)
        result["expectedHumanEpoch"] = current["humanEpoch"]
        result["expectedWorldRevision"] = current["worldRevision"]
        result["targetProof"] = {
            "x": result["x"], "y": result["y"], "width": result["width"],
            "height": result["height"], "pixel_sha256": result["pixelSha256"],
            "focused_window": result.get("focusedWindow"),
        }
        response = action_result(accepted=True, action_id=action_id, state=current,
            actuator="screen.x11grab", summary="captured screenshot", data=result,
            evidence_items=[evidence("screenshot", **result)])
        response["endingWorldRevision"] = current["worldRevision"]
        return response
