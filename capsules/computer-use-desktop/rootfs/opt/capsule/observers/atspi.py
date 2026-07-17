"""Bounded AT-SPI observation with graceful unavailable reporting."""

from __future__ import annotations

import json
import os
import subprocess
import sys

# This module runs both as an import (desktopd) AND as a script-by-path subprocess
# (see the /usr/bin/python3 __file__ self-invocations below). In the subprocess case
# Python puts observers/ on sys.path[0], NOT the capsule dir, so sibling-package imports
# like atspi_compact/evidence would fail. Add the capsule dir (parent of observers/).
_CAPSULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CAPSULE_DIR not in sys.path:
    sys.path.insert(0, _CAPSULE_DIR)

from atspi_compact import compact_nodes, is_inert
from evidence import redact_text


class AtspiObserver:
    def __init__(self, max_nodes=500, max_depth=8):
        self.max_nodes = max(1, min(int(max_nodes), 2000))
        self.max_depth = max(1, min(int(max_depth), 16))

    def _module(self):
        try:
            import pyatspi
            return ("pyatspi", pyatspi)
        except ImportError:
            try:
                import gi
                gi.require_version("Atspi", "2.0")
                from gi.repository import Atspi
                Atspi.init()
                return ("gi", Atspi)
            except (ImportError, ValueError):
                return None

    def _desktop(self, backend):
        flavor, module = backend
        return module.Registry.getDesktop(0) if flavor == "pyatspi" else module.get_desktop(0)

    def _children(self, node, flavor):
        if flavor == "pyatspi":
            children = []
            for index, child in enumerate(node):
                if index >= 200:
                    break
                children.append(child)
            return children
        return [node.get_child_at_index(index) for index in range(min(node.get_child_count(), 200))]

    def _name(self, node, flavor):
        return str((node.name if flavor == "pyatspi" else node.get_name()) or "")

    def _role(self, node, flavor):
        return str(node.getRoleName() if flavor == "pyatspi" else node.get_role_name())

    def available(self):
        backend = self._module()
        if not backend:
            try:
                value = subprocess.run(["/usr/bin/python3", __file__, "available", "", "", "", "1", "1"],
                                       capture_output=True, text=True, timeout=2, check=True)
                return json.loads(value.stdout)
            except Exception:
                return False
        try:
            self._desktop(backend)
            return True
        except Exception:
            return False

    def tree(self, app_name="", role="", name=""):
        backend = self._module()
        if not backend:
            try:
                value = subprocess.run(["/usr/bin/python3", __file__, "tree", app_name, role, name,
                                        str(self.max_nodes), str(self.max_depth)], capture_output=True,
                                       text=True, timeout=4, check=True)
                return json.loads(value.stdout)
            except Exception:
                return {"available": False, "nodes": [], "truncated": False,
                        "warnings": ["atspi_unavailable"]}
        flavor, atspi = backend
        desktop = self._desktop(backend)
        nodes, truncated, visited = [], False, 0

        def visit(node, depth, app_identity):
            nonlocal truncated, visited
            if visited >= self.max_nodes:
                truncated = True
                return
            visited += 1
            try:
                node_name = redact_text(self._name(node, flavor), limit=500)
                node_role = self._role(node, flavor)[:100]
                states = node.getState() if flavor == "pyatspi" else node.get_state_set()
                visible_state = atspi.STATE_VISIBLE if flavor == "pyatspi" else atspi.StateType.VISIBLE
                showing_state = atspi.STATE_SHOWING if flavor == "pyatspi" else atspi.StateType.SHOWING
                visible = states.contains(visible_state) if states else False
                showing = states.contains(showing_state) if states else False
                actions = []
                try:
                    action_iface = node.queryAction() if flavor == "pyatspi" else node.get_action_iface()
                    count = action_iface.nActions if flavor == "pyatspi" else action_iface.get_n_actions()
                    actions = [str(action_iface.getName(i) if flavor == "pyatspi" else action_iface.get_action_name(i))[:100]
                               for i in range(min(count, 20))]
                except Exception:
                    pass
                if ((not role or role.lower() == node_role.lower()) and
                    (not name or name.lower() in node_name.lower())):
                    candidate = {"appIdentity": app_identity[:200], "name": node_name,
                                 "role": node_role, "visible": bool(visible),
                                 "showing": bool(showing), "actions": actions,
                                 "depth": depth}
                    # A11y-Compressor: skip inert scaffolding (unnamed, action-less,
                    # non-operable) so the max_nodes budget is spent on signal, not noise.
                    # An explicit role/name filter means the caller wants that exact slice,
                    # so don't second-guess it there.
                    if (role or name) or not is_inert(candidate):
                        nodes.append(candidate)
                if depth < self.max_depth:
                    for child in self._children(node, flavor):
                        visit(child, depth + 1, app_identity)
            except Exception:
                return

        for app in self._children(desktop, flavor)[:100]:
            identity = self._name(app, flavor)
            if app_name and app_name.strip().casefold() != identity.strip().casefold():
                continue
            visit(app, 0, identity)
        # Final dedup pass (collapse runs of identical action-less siblings). Inert nodes
        # were already skipped during traversal above; this catches duplicate signal.
        nodes, stats = compact_nodes(nodes, drop_inert=False, dedup=True)
        return {"available": True, "nodes": nodes, "truncated": truncated,
                "compaction": stats, "warnings": []}

    def invoke(self, app_name, role, name, action):
        backend = self._module()
        if not backend:
            value = subprocess.run(["/usr/bin/python3", __file__, "invoke", app_name, role, name, action,
                                    str(self.max_nodes), str(self.max_depth)], capture_output=True,
                                   text=True, timeout=4, check=True)
            return json.loads(value.stdout)
        flavor, _ = backend
        desktop = self._desktop(backend)
        matches, visited = [], 0
        for app in self._children(desktop, flavor)[:100]:
            identity = self._name(app, flavor)
            if app_name and app_name.strip().casefold() != identity.strip().casefold():
                continue
            queue = [(app, 0)]
            while queue and len(matches) < 2 and visited < self.max_nodes:
                node, depth = queue.pop(0)
                visited += 1
                try:
                    if (self._role(node, flavor).lower() == role.lower() and
                            self._name(node, flavor).lower() == name.lower()):
                        matches.append(node)
                    if depth < self.max_depth:
                        queue.extend((child, depth + 1) for child in self._children(node, flavor))
                except Exception:
                    pass
        if len(matches) != 1:
            raise ValueError("accessibility selector did not resolve uniquely")
        # Screen extents of the target, for visible presentation (the bridge glides the real cursor
        # to the CENTER of the element the agent acted on). Best-effort — never blocks the action.
        screen_target = None
        try:
            comp = matches[0].queryComponent() if flavor == "pyatspi" else matches[0].get_component_iface()
            if flavor == "pyatspi":
                import pyatspi
                ext = comp.getExtents(pyatspi.DESKTOP_COORDS)
                sx, sy, sw, sh = ext.x, ext.y, ext.width, ext.height
            else:
                ext = comp.get_extents(1)  # ATSPI_COORD_TYPE_SCREEN
                sx, sy, sw, sh = ext.x, ext.y, ext.width, ext.height
            if sw > 0 and sh > 0:
                screen_target = {"x": int(sx + sw / 2), "y": int(sy + sh / 2)}
        except Exception:
            pass
        iface = matches[0].queryAction() if flavor == "pyatspi" else matches[0].get_action_iface()
        count = iface.nActions if flavor == "pyatspi" else iface.get_n_actions()
        for index in range(min(count, 20)):
            action_name = iface.getName(index) if flavor == "pyatspi" else iface.get_action_name(index)
            if action_name.lower() == action.lower():
                committed = iface.doAction(index) if flavor == "pyatspi" else iface.do_action(index)
                if not committed:
                    raise RuntimeError("accessibility action was refused")
                result = {"appIdentity": identity[:200], "role": role, "name": name, "action": action}
                if screen_target:
                    result["screenTarget"] = screen_target
                return result
        raise ValueError("accessibility action is not supported by target")


if __name__ == "__main__":
    operation, app, role, name = sys.argv[1:5]
    if operation == "available":
        observer = AtspiObserver(1, 1); result = observer.available()
    elif operation == "tree":
        observer = AtspiObserver(int(sys.argv[5]), int(sys.argv[6])); result = observer.tree(app, role, name)
    else:
        observer = AtspiObserver(int(sys.argv[6]), int(sys.argv[7])); result = observer.invoke(app, role, name, sys.argv[5])
    print(json.dumps(result, separators=(",", ":")))
