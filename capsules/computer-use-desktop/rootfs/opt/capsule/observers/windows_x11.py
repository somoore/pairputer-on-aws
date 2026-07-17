"""Bounded EWMH window observation through python-xlib."""

from evidence import redact_text

class X11WindowObserver:
    def __init__(self, display: str = ":1"):
        self.display = display

    def list_windows(self, limit: int = 100):
        try:
            from Xlib import X, display
            connection = display.Display(self.display)
            root = connection.screen().root
            atom = connection.intern_atom("_NET_CLIENT_LIST")
            prop = root.get_full_property(atom, X.AnyPropertyType)
            ids = list(prop.value) if prop is not None else [child.id for child in root.query_tree().children]
        except Exception:
            return [], ["x11_unavailable"]
        windows = []
        for ident in ids[:max(1, min(limit, 200))]:
            try:
                window = connection.create_resource_object("window", int(ident))
                geometry = window.get_geometry()
                point = window.translate_coords(root, 0, 0)
                wm_class = window.get_wm_class() or ("", "")
                windows.append({"windowId": hex(int(ident)), "desktop": 0,
                                "x": int(point.x), "y": int(point.y),
                                "width": int(geometry.width), "height": int(geometry.height),
                                "host": "localhost", "appIdentity": redact_text(".".join(wm_class), limit=200),
                                "title": redact_text(str(window.get_wm_name() or ""), limit=500),
                                "provenance": "untrusted_x11", "authoritative": False})
            except Exception:
                continue
        connection.close()
        return windows, []

    def focus(self, window_id):
        from Xlib import X, display, protocol
        connection = display.Display(self.display); root = connection.screen().root
        ident = int(str(window_id), 16)
        window = connection.create_resource_object("window", ident)
        event = protocol.event.ClientMessage(window=window, client_type=connection.intern_atom("_NET_ACTIVE_WINDOW"),
                                              data=(32, [1, X.CurrentTime, 0, 0, 0]))
        root.send_event(event, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        # Some lightweight WMs accept the EWMH activation but leave X focus at PointerRoot. Pin the
        # exact selected client as the server input focus so screenshot-bound keyboard consent can
        # prove and revalidate the real key recipient before every injected key.
        window.set_input_focus(X.RevertToPointerRoot, X.CurrentTime)
        connection.sync(); connection.close()
