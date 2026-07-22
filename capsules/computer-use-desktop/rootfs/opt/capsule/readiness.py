#!/usr/bin/env python3.11
"""Loopback lifecycle/readiness endpoint. Required invariants fail closed."""
import grp, http.client, json, os, re, socket, stat, subprocess, tempfile, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

READY_FLAG="/run/capsule.ready"
BRIDGE_KEY_FILE=os.environ.get("PAIRPUTER_BRIDGE_CAPABILITY_FILE","/run/pairputer/bridge-ingress.key")
MAX_HOOK_BODY=20*1024
MAX_DEBUG_LOG_CHUNK=256*1024
DEBUG_LOGS={
 "/dbg/inputws":"/var/log/pairputer-input-ws.log",
 "/dbg/bridge":"/var/log/pairputer-agent-bridge.log",
 "/dbg/session":"/var/log/pairputer-session.log",
}
CAPABILITY_RE=re.compile(r"^[A-Za-z0-9_-]{43,256}$")
STATE_LOCK=threading.Lock()
STATE={"ready":False,"checks":{},"observedAt":0.0}
RUN_HOOK_LOCK=threading.Lock()
RUN_HOOK_ACCEPTED=False
RUN_HOOK_DISABLED=os.environ.get("PAIRPUTER_DISABLE_RUN_HOOK_REKEY","false").lower() in {"1","true","yes"}

def debug_log_chunk(path,offset):
 if path not in DEBUG_LOGS:raise ValueError("unknown debug log")
 if isinstance(offset,bool) or not isinstance(offset,int) or offset<0:raise ValueError("invalid debug offset")
 flags=os.O_RDONLY|os.O_CLOEXEC
 if hasattr(os,"O_NOFOLLOW"):flags|=os.O_NOFOLLOW
 try:fd=os.open(DEBUG_LOGS[path],flags)
 except FileNotFoundError:return {"offset":0,"size":0,"data":""}
 try:
  info=os.fstat(fd)
  if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077:
   raise PermissionError("unsafe debug log")
  start=offset if offset<=info.st_size else 0
  os.lseek(fd,start,os.SEEK_SET)
  data=os.read(fd,MAX_DEBUG_LOG_CHUNK)
  return {"offset":start,"size":info.st_size,"data":data.decode("utf-8","replace")}
 finally:os.close(fd)

def bridge_capability_ready():
 try:
  stat=os.stat(BRIDGE_KEY_FILE,follow_symlinks=False)
  with open(BRIDGE_KEY_FILE,encoding="ascii") as handle:value=handle.read(257).strip()
  return (stat.st_uid==0 and stat.st_gid==grp.getgrnam("agent").gr_gid and
          stat.st_mode&0o777==0o640 and bool(CAPABILITY_RE.fullmatch(value)))
 except Exception:return False

def install_bridge_capability(value):
 if not isinstance(value,str) or not CAPABILITY_RE.fullmatch(value):
  raise ValueError("invalid bridge capability")
 directory=os.path.dirname(BRIDGE_KEY_FILE)
 os.makedirs(directory,mode=0o755,exist_ok=True)
 fd,temporary=tempfile.mkstemp(prefix=".bridge-ingress-",dir=directory)
 try:
  os.fchmod(fd,0o640);os.fchown(fd,0,grp.getgrnam("agent").gr_gid)
  handle=os.fdopen(fd,"w",encoding="ascii",closefd=True);fd=-1
  with handle:
   handle.write(value+"\n");handle.flush();os.fsync(handle.fileno())
  os.replace(temporary,BRIDGE_KEY_FILE)
  os.chmod(BRIDGE_KEY_FILE,0o640,follow_symlinks=False)
  os.chown(BRIDGE_KEY_FILE,0,grp.getgrnam("agent").gr_gid,follow_symlinks=False)
 finally:
  if fd>=0:os.close(fd)
  try:os.unlink(temporary)
  except FileNotFoundError:pass

def accept_run_hook(raw):
 global RUN_HOOK_ACCEPTED
 if RUN_HOOK_DISABLED:raise PermissionError("run hook rekeying is disabled")
 with RUN_HOOK_LOCK:
  if RUN_HOOK_ACCEPTED:raise FileExistsError("run hook was already accepted")
  _accept_run_hook_once(raw)
  RUN_HOOK_ACCEPTED=True

def _accept_run_hook_once(raw):
 outer=json.loads(raw)
 if not isinstance(outer,dict):raise ValueError("run hook body must be an object")
 payload=outer.get("runHookPayload")
 if not isinstance(payload,str) or len(payload.encode("utf-8"))>16384:
  raise ValueError("run hook payload is invalid")
 value=json.loads(payload)
 if not isinstance(value,dict) or set(value)!={"bridgeCapability"}:
  raise ValueError("run hook payload schema is invalid")
 install_bridge_capability(value["bridgeCapability"])

def port(port):
 try:
  with socket.create_connection(("127.0.0.1",port),.5):return True
 except OSError:return False

def command(argv,timeout=2):
 try:return subprocess.run(argv,capture_output=True,timeout=timeout,check=False).returncode==0
 except Exception:return False

def cdp():
 try:
  with urllib.request.urlopen("http://127.0.0.1:9222/json/version",timeout=1) as response:
   value=json.loads(response.read(65536));return bool(value.get("webSocketDebuggerUrl"))
 except Exception:return False

def egress_proxy():
 try:
  connection=http.client.HTTPConnection("127.0.0.1",int(os.environ.get("PAIRPUTER_EGRESS_PROXY_PORT","6907")),timeout=1)
  connection.request("GET","/health");response=connection.getresponse();response.read();connection.close()
  return response.status==204 and command(["pgrep","-u","egressd","-f","/opt/capsule/egress_proxy.py"])
 except Exception:return False

def grpc_ready():
 try:
  import grpc
  from desktopgen.pairputer.desktop.v1 import desktop_pb2,desktop_pb2_grpc
  with open(os.environ.get("PAIRPUTER_DESKTOP_AGENT_KEY_FILE","/run/pairputer/desktop-agent.key"),encoding="utf-8") as key:
   metadata=(("authorization","Bearer "+key.read().strip()),)
  with grpc.insecure_channel("127.0.0.1:50051") as channel:
   value=desktop_pb2_grpc.DesktopAgentStub(channel).GetCapabilities(desktop_pb2.GetCapabilitiesRequest(),timeout=1,metadata=metadata)
  return value.protocol_version=="pairputer.desktop.v1"
 except Exception:return False

def rendered():
 try:
  from Xlib import X,display
  d=display.Display(":1");s=d.screen();w,h=min(s.width_in_pixels,320),min(s.height_in_pixels,200)
  data=s.root.get_image((s.width_in_pixels-w)//2,(s.height_in_pixels-h)//2,w,h,X.ZPixmap,0xffffffff).data;d.close()
  return bool(data) and max(data)>12 and sum(data)//len(data)>2
 except Exception:return False

def ffmpeg_capture():
 # Xlib rendered() proves X has pixels, but observe/screenshot use ffmpeg x11grab — a slower path
 # that lagged behind at boot, so the VM reported ready while screenshots still timed out. Gate on a
 # real single-frame x11grab so RUNNING means the capture path callers actually use works.
 try:
  proc=subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-probesize","32",
   "-thread_queue_size","8","-f","x11grab","-i",":1.0","-frames:v","1","-f","image2pipe",
   "-vcodec","png","pipe:1"],capture_output=True,timeout=12,check=False)
  return proc.returncode==0 and bool(proc.stdout)
 except Exception:return False

def atspi():
 try:
  from observers.atspi import AtspiObserver
  result=AtspiObserver(max_nodes=200,max_depth=5).tree()
  return result["available"] and any(n.get("name") for n in result["nodes"])
 except Exception:return False

def workspace():
 try:
  probe=("import os,tempfile; d='/home/app/workspace'; "
         "fd,p=tempfile.mkstemp(prefix='.readiness-',dir=d); "
         "os.write(fd,b'ok'); os.fsync(fd); os.close(fd); "
         "assert open(p,'rb').read()==b'ok'; os.unlink(p)")
  return subprocess.run(["runuser","-u","app","--","python3.11","-c",probe],
                        capture_output=True,timeout=2,check=False).returncode==0
 except Exception:return False

def journal():
 try:
  path=os.environ.get("PAIRPUTER_DESKTOP_BRAIN_DB","/var/lib/pairputer-brain/brain.sqlite3")
  probe=("import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
         "assert c.execute('PRAGMA quick_check').fetchone()[0]=='ok'; "
         "assert c.execute(\"SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'\").fetchone(); c.close()")
  return subprocess.run(["runuser","-u","agent","--","python3.11","-c",probe,path],
                        capture_output=True,timeout=2,check=False).returncode==0
 except Exception:return False

def checks():
 # NOTE: chromium is NOT gated. The browser must NOT auto-open at boot — it launches ONLY when the
 # human clicks the dock or the model calls apps_open("browser"). So a running Chromium can't be a
 # readiness precondition (that's the whole reason it used to be force-launched at boot and maximized
 # over the desktop). apps_open verifies the browser at open-time instead. chromium_cdp/visible stay
 # as INFORMATIONAL signals below.
 required={"x_display":os.path.exists("/tmp/.X11-unix/X1"),"window_manager":command(["pgrep","-x","mutter"]),
  "dbus_session":command(["pgrep","-f","dbus-(broker|daemon).*(session|nofork)"]),
  "atspi":atspi(),"video":port(6903),"audio":port(6902),"input":port(6904),"bridge":port(6905),
  "coplay":port(6906),"egress_proxy":egress_proxy(),"grpc":grpc_ready(),
  "bridge_capability":bridge_capability_ready(),
  "terminal_tmux":command(["runuser","-u","terminal","--","tmux","-f","/dev/null","has-session","-t","workbench"]),
  "workspace":workspace(),"journal":journal(),"rendered_frame":rendered(),
  "ffmpeg_capture":ffmpeg_capture(),
  "xtest":command(["python3.11","/opt/capsule/input_selftest.py"],3)}
 return required

# Informational (NOT gating): reported in /ready checks for visibility, but never a precondition — the
# desktop is usable without the human dock, and the browser is intentionally not running until opened.
def info_checks():
 return {"launcher_panel":command(["pgrep","-f","/opt/capsule/launcher-panel.py"]),
         "chromium_cdp":cdp(),"chromium_visible":command(["pgrep","-f","/opt/chromium/chrome"])}

def monitor():
 while True:
  values=checks();ready=all(values.values());observed=time.time()
  values=dict(values,**info_checks())
  with STATE_LOCK:
   STATE.update({"ready":ready,"checks":values,"observedAt":observed})
  if ready:
   fd=os.open(READY_FLAG,os.O_WRONLY|os.O_CREAT|os.O_CLOEXEC,0o644);os.close(fd)
  else:
   try:os.unlink(READY_FLAG)
   except FileNotFoundError:pass
  time.sleep(5 if ready else 1)

class Handler(BaseHTTPRequestHandler):
 def _send_json(self,status,payload):
  body=json.dumps(payload,separators=(",",":")).encode()
  self.send_response(status);self.send_header("Content-Type","application/json")
  self.send_header("Content-Length",str(len(body)));self.send_header("Cache-Control","no-store")
  self.end_headers()
  try:self.wfile.write(body)
  except (BrokenPipeError,ConnectionResetError):pass
 def do_GET(self):
  parsed=urllib.parse.urlparse(self.path)
  if parsed.path in DEBUG_LOGS:
   try:
    values=urllib.parse.parse_qs(parsed.query,strict_parsing=False)
    if set(values)-{"offset"}:raise ValueError("unknown debug query")
    raw=(values.get("offset") or ["0"])[0]
    if not raw.isdigit() or len(raw)>20:raise ValueError("invalid debug offset")
    self._send_json(200,debug_log_chunk(parsed.path,int(raw)))
   except (ValueError,PermissionError):self._send_json(400,{"error":"invalid_debug_request"})
   return
  with STATE_LOCK:snapshot=dict(STATE);snapshot["checks"]=dict(STATE["checks"])
  # The image builder snapshots this process only after the monitor has proved
  # every invariant.  A restored Run hook can therefore answer immediately
  # from coherent snapshotted state while the monitor continues to revoke the
  # flag if any service later degrades.
  ready=bool(snapshot["ready"] and os.path.exists(READY_FLAG))
  self._send_json(200 if ready else 503,{"status":"ok" if ready else "starting","checks":snapshot["checks"],"observedAt":snapshot["observedAt"]})
 def do_POST(self):
  path=self.path.split("?",1)[0]
  if path in {"/run","/aws/lambda-microvms/runtime/v1/run"}:
   try:
    length=int(self.headers.get("Content-Length","0"))
    if length<2 or length>MAX_HOOK_BODY:raise ValueError("run hook body is invalid")
    accept_run_hook(self.rfile.read(length))
    self._send_json(200,{"ok":True})
   except FileExistsError:
    self._send_json(409,{"ok":False,"error":"run_hook_already_accepted"})
   except PermissionError:
    self._send_json(403,{"ok":False,"error":"run_hook_disabled"})
   except Exception:
    self._send_json(503,{"ok":False,"error":"run_hook_rejected"})
   return
  if path in {"/resume","/aws/lambda-microvms/runtime/v1/resume"}:
   self._send_json(200 if bridge_capability_ready() else 503,
                   {"ok":bridge_capability_ready()})
   return
  self.do_GET()
 def log_message(self,*args):pass

if __name__=="__main__":
 try:os.unlink(READY_FLAG)
 except FileNotFoundError:pass
 threading.Thread(target=monitor,name="readiness-monitor",daemon=True).start()
 # 127.0.0.1 ONLY: capsuled probes :9000 over the CH vsock (the ee968da forwarder does
 # vsock:9000->127.0.0.1:9000), so :9000 + the /dbg logs must NOT be on the tap — a peer VM or the
 # guest network could otherwise read another tenant's boot diagnostics (the cross-tenant boundary
 # the vsock readiness design exists to close). Matches agent-doom hook.py. Default loopback; the
 # env override stays for any host-direct debug use.
 ThreadingHTTPServer((os.environ.get("PAIRPUTER_READY_BIND","127.0.0.1"),9000),Handler).serve_forever()
