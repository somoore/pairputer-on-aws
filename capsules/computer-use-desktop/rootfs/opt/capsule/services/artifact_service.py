from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .common import action_result, evidence, mime_for, require_action_envelope, sha256_bytes
from .control_state import LeaseRejected


class ArtifactService:
    def __init__(self, workspace, control, registry_path=None):
        self.workspace, self.control = workspace, control
        self.registry_path = Path(registry_path or (workspace.state_dir / "artifacts.json"))

    def export(self, request):
        action_id, epoch, revision, _ = require_action_envelope(request)
        parts = self.workspace._parts(str(request.get("path", "")))
        data = self.workspace._file_bytes(parts)
        digest = sha256_bytes(data)
        expected = request.get("expected_sha256")
        if expected != digest:
            raise ValueError("artifact expected_sha256 mismatch")
        artifact_id = str(uuid.uuid4())
        try:
            with self.control.commit(epoch, revision) as state:
                try:
                    registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    registry = []
                entry = {"artifactId": artifact_id, "path": "/".join(parts), "sha256": digest,
                         "mimeType": mime_for(parts[-1]), "size": len(data), "registeredAt": time.time()}
                registry = (registry + [entry])[-500:]
                tmp = self.registry_path.with_name(f".{self.registry_path.name}.{uuid.uuid4().hex}")
                tmp.write_text(json.dumps(registry, sort_keys=True, separators=(",", ":")), encoding="utf-8")
                os.chmod(tmp, 0o600); os.replace(tmp, self.registry_path)
                return action_result(accepted=True, action_id=action_id, state=state,
                    actuator="artifact.registry", summary=f"registered artifact {'/'.join(parts)}", data=entry,
                    evidence_items=[evidence("artifact_registered", **entry)])
        except LeaseRejected as exc:
            return action_result(accepted=False, action_id=action_id, state=exc.state,
                                 actuator="artifact.registry", summary="artifact export rejected", reason=exc.reason)
