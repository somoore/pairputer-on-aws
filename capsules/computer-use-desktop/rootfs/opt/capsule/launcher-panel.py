#!/usr/bin/env python3
"""A minimal human-facing app launcher for the Pairputer Workbench desktop.

Run with system python3 (3.9), NOT python3.11 — PyGObject (gi) is only installed for 3.9
(the python3-gobject RPM); python3.11 has no gi. python-xlib (3.11-only) is imported defensively
below, so the strut is simply skipped here and the dock still shows via its DOCK hint + keep_above.

The desktop is driven by the AI through the typed app_service (apps_open), so it ships with no panel,
taskbar, or icons — which leaves a *human* who takes the wheel with no visible way to open anything.
This is that missing affordance: a top dock bar of launch buttons that no window can cover.

It's a real dock: type-hint DOCK, spans the screen width at the top, and reserves strut space via
`_NET_WM_STRUT`/`_NET_WM_STRUT_PARTIAL` so mutter tiles/maximizes app windows BELOW it (a maximized
Chromium used to cover a floating bar in the corner — walls #24). Runs as the `app` user inside the
`app` desktop session (session.sh) and launches exactly the apps that session already owns as that
user; it never touches desktopd, root, or the terminal principal. No new packages: GTK3 +
python3-gobject are already in the image. ponytail: a dock, not a desktop environment.
"""
from __future__ import annotations

import shutil
import subprocess

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

try:  # python-xlib (installed image-wide) sets the strut reliably; keep_above is the fallback.
    from Xlib import display as xdisplay, Xatom
except Exception:  # noqa: BLE001
    xdisplay = None

BAR_HEIGHT = 40  # px reserved at the top of the screen

# label -> argv. Plain text labels (the image has no emoji font — glyphs render as tofu). The browser
# is NOT running until asked; Browser/VS Code launch it on demand (single-instance, so a repeat click
# raises the existing window). VS Code opens Chromium onto the loopback code-server preview.
BUTTONS = [
    ("Files", ["nautilus", "--no-desktop"]),
    ("Editor", ["gnome-text-editor"]),
    ("Browser", ["/usr/local/bin/pairputer-chromium"]),
    ("VS Code", ["/usr/local/bin/pairputer-chromium", "http://127.0.0.1:4500"]),
    ("Terminal", ["xterm", "-e", "/bin/bash"]),
]


def launch(argv):
    exe = shutil.which(argv[0]) or argv[0]
    try:
        subprocess.Popen([exe, *argv[1:]], stdin=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except Exception:
        pass  # a failed launch must never crash the bar — the human just clicks again


def reserve_strut(win, width):
    """Reserve `BAR_HEIGHT` px at the top via _NET_WM_STRUT[_PARTIAL] so mutter shrinks the workarea
    and any 'maximized' app window (Chromium) tiles BELOW the dock instead of over it (walls #24)."""
    if xdisplay is None:
        return
    try:
        gdk_win = win.get_window()
        xid = gdk_win.get_xid()
        d = xdisplay.Display()
        w = d.create_resource_object("window", xid)
        strut_partial = d.intern_atom("_NET_WM_STRUT_PARTIAL")
        strut = d.intern_atom("_NET_WM_STRUT")
        # left,right,top,bottom + 8 start/end pairs; we reserve only the top, spanning the width.
        w.change_property(strut_partial, Xatom.CARDINAL, 32,
                          [0, 0, BAR_HEIGHT, 0, 0, 0, 0, 0, 0, width - 1, 0, 0])
        w.change_property(strut, Xatom.CARDINAL, 32, [0, 0, BAR_HEIGHT, 0])
        d.sync()
    except Exception:
        pass  # strut is best-effort; keep_above still keeps the bar visible if this fails


def main():
    screen = Gdk.Screen.get_default()
    width = screen.get_width() if screen else 1440

    win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
    win.set_title("Pairputer")
    win.set_decorated(False)
    win.set_resizable(False)
    win.set_skip_taskbar_hint(True)
    win.set_skip_pager_hint(True)
    win.set_keep_above(True)
    win.set_type_hint(Gdk.WindowTypeHint.DOCK)
    win.set_size_request(width, BAR_HEIGHT)
    win.move(0, 0)

    win.get_style_context().add_class("pairputer-dock")
    css = Gtk.CssProvider()
    css.load_from_data(b".pairputer-dock{background:#1b1e24;} "
                       b".pairputer-dock button{padding:2px 12px;margin:0 3px;}")
    Gtk.StyleContext.add_provider_for_screen(screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    bar.set_margin_top(3); bar.set_margin_bottom(3)
    bar.set_margin_start(8); bar.set_margin_end(8)
    brand = Gtk.Label(label="Pairputer")
    brand.set_margin_end(10)
    bar.pack_start(brand, False, False, 0)
    for label, argv in BUTTONS:
        b = Gtk.Button(label=label)
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.connect("clicked", lambda _btn, a=argv: launch(a))
        bar.pack_start(b, False, False, 0)
    win.add(bar)
    win.connect("destroy", Gtk.main_quit)

    def on_realize(_w):
        reserve_strut(win, width)
    win.connect("realize", on_realize)
    win.show_all()
    # Re-assert placement + strut a moment after mutter maps/restacks us.
    def settle():
        win.move(0, 0)
        win.set_keep_above(True)
        reserve_strut(win, width)
        return False
    GLib.timeout_add(1500, settle)
    Gtk.main()


if __name__ == "__main__":
    main()
