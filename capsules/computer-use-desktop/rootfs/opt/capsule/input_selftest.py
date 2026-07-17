#!/usr/bin/env python3.11
import sys,time
from Xlib import X,XK,display
from Xlib.ext import xtest
def main():
 d=display.Display(":1")
 if not d.query_extension("XTEST"):return 2
 root=d.screen().root; win=root.create_window(0,0,16,16,0,d.screen().root_depth,X.InputOutput,X.CopyFromParent,override_redirect=1,event_mask=X.KeyPressMask)
 win.map();d.sync();time.sleep(.1);code=d.keysym_to_keycode(XK.XK_a)
 for _ in range(3):
  grabbed=False
  try:grabbed=win.grab_keyboard(True,X.GrabModeAsync,X.GrabModeAsync,X.CurrentTime)==X.GrabSuccess
  except Exception:pass
  if not grabbed:win.set_input_focus(X.RevertToParent,X.CurrentTime)
  d.sync();time.sleep(.05);xtest.fake_input(d,X.KeyPress,code);d.sync();xtest.fake_input(d,X.KeyRelease,code);d.sync()
  end=time.time()+1
  while time.time()<end:
   while d.pending_events():
    if d.next_event().type==X.KeyPress:
     try:d.ungrab_keyboard(X.CurrentTime)
     except Exception:pass
     win.destroy();d.sync();return 0
   time.sleep(.02)
  try:d.ungrab_keyboard(X.CurrentTime)
  except Exception:pass
 win.destroy();d.sync();return 1
if __name__=="__main__":sys.exit(main())
