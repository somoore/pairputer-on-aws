from .common import action_result, evidence, require_action_envelope
from .control_state import LeaseRejected


class AccessibilityService:
    def __init__(self, control, observer, allowed_apps=None):
        self.control, self.observer = control, observer
        self.allowed_apps = set(allowed_apps or {"Chromium", "Text Editor", "Files", "xterm"})

    def tree(self, app_name="", role="", name=""):
        if not app_name or app_name not in self.allowed_apps:
            raise ValueError("application is outside accessibility policy")
        return {"ok": True, **self.observer.tree(app_name, role, name), **self.control.snapshot()}

    def action(self, request):
        action_id, epoch, revision, _ = require_action_envelope(request)
        app, role, name, operation = (str(request.get(key, "")) for key in
                                      ("app_name", "role", "name", "operation"))
        if app not in self.allowed_apps or not all((role, name, operation)):
            raise ValueError("invalid or out-of-scope accessibility selector")
        try:
            with self.control.commit(epoch, revision) as state:
                result = self.observer.invoke(app, role, name, operation)
                return action_result(accepted=True, action_id=action_id, state=state,
                    actuator="atspi.action", summary=f"invoked {operation} on {name}", data=result,
                    evidence_items=[evidence("accessibility_action", **result)])
        except LeaseRejected as exc:
            return action_result(accepted=False, action_id=action_id, state=exc.state,
                                 actuator="atspi.action", summary="accessibility action rejected", reason=exc.reason)
