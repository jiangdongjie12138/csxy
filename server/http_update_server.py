# http_update_server.py
# Local HTTP update server that stands in for update.csxy.163.com during the
# launcher update phase. Content is based on the *real* files archived from
# update.csxy.163.com (evidence/netease/), with the login-server IP rewritten
# to LOGIN_HOST so the game client is steered to our local listener.
#
# Endpoints emulated:
#   /server3.txt    - server list, written by csxylaunch into server.ini
#   /server1.txt    - alternate server list (older launcher default)
#   /patch_list1.txt- patch index; client version is the last entry -> no patch
#   /note.txt       - announcement (404 upstream; we return a blank 200 stub)
# Every request is logged to logs/requests.log for observation back-verification.
#
import os, re, socket, sys, datetime, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# v0.2: added version banner (fingerprint + source sha1 self-print). Behaviour
# unchanged from the real-client-verified v0.1.
VERSION = "0.2.0"
FINGERPRINT = "csxy-http-v0.2-e4fc07ab"
import simmeta

BASE = os.path.dirname(os.path.abspath(__file__))
EVID = os.environ.get("CSXY_EVIDENCE", os.path.join(BASE, "evidence", "netease"))
LOGDIR = os.path.join(BASE, "logs")
os.makedirs(LOGDIR, exist_ok=True)

HTTP_PORT = int(os.environ.get("HTTP_PORT", "8000"))
HOST = os.environ.get("HTTP_BIND", "0.0.0.0")
# Address we want the game client to connect to for the login server.
# Default 127.0.0.1 works when client+server share the login loopback or local host;
# point this at the actual server IP when running the client in a VM.
LOGIN_HOST = os.environ.get("LOGIN_HOST", "127.0.0.1")
CLIENT_VERSION = os.environ.get("CLIENT_VERSION", "1.1.179")

_LOG = os.path.join(LOGDIR, "requests.log")

def nowtext():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _load(path):
    if not os.path.isfile(path):
        return None
    return open(path, "rb").read()

def build_serverlist_bytes():
    """Take the real server3.txt and rewrite every dotted IP to LOGIN_HOST."""
    raw = _load(os.path.join(EVID, "http_server3.txt"))
    if raw is None:
        return None
    text = raw.decode("gbk", "replace")
    fixed = re.sub(r"\d{1,3}(?:\.\d{1,3}){3}", LOGIN_HOST, text)
    return fixed.encode("gbk")

def build_patchlist_bytes():
    """Client version is the last real entry, so return a patch index whose
    only entry equals the local version -> launcher sees no pending update."""
    host = LOGIN_HOST
    port = HTTP_PORT
    body = (
        "%s http://%s:%d/patch%s.csxy\n"
        "#bturl http://%s:%d/\n"
        "#timeout_to_http 0\n" % (CLIENT_VERSION, host, port, CLIENT_VERSION, host, port)
    )
    return body.encode("ascii")

def serverlist_bytes():
    b = _load(os.path.join(BASE, "webroot", "server3.txt"))
    if b is None:
        return build_serverlist_bytes()
    # The archived list contains the address captured during analysis.  Always
    # rewrite it at request time so a VM can use LOGIN_HOST without editing the
    # repository copy of server3.txt.
    text = b.decode("gbk", "replace")
    return re.sub(r"\d{1,3}(?:\.\d{1,3}){3}", LOGIN_HOST, text).encode("gbk")

def patchlist_bytes():
    b = _load(os.path.join(BASE, "webroot", "patch_list1.txt"))
    return b if b is not None else build_patchlist_bytes()

def note_bytes():
    b = _load(os.path.join(BASE, "webroot", "note.txt"))
    return b if b is not None else b"\n"

ROUTES = {
    "/server3.txt": ("server3.txt", serverlist_bytes),
    "/server1.txt": ("text/plain", serverlist_bytes),
    "/patch_list1.txt": ("text/plain", patchlist_bytes),
    "/patch%s.csxy" % CLIENT_VERSION: ("application/octet-stream", lambda: b""),
    "/note.txt": ("text/plain", note_bytes),
}

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _log_req(self, extra=""):
        line = "%s | %s | %s | host=%s | ua=%s | ref=%s | ver=%s%s\n" % (
            nowtext(), self.client_address, self.command + " " + self.path,
            self.headers.get("Host", ""), self.headers.get("User-Agent", ""),
            self.headers.get("Referer", ""), self.request_version, extra)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(line)
        sys.stdout.write(line)
        sys.stdout.flush()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        # support trailing slash variants for root
        if path in ("/", ""):
            path = "/server3.txt"
        entry = ROUTES.get(path)
        if entry is None:
            self._log_req(" -> 404")
            self.send_response(404)
            self.end_headers()
            return
        ctype, getter = entry
        body = getter()
        if body is None:
            self._log_req(" -> 404 (generator returned None)")
            self.send_response(404)
            self.end_headers()
            return
        self._log_req(" -> 200 len=%d (%s)" % (len(body), ctype))
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "GET":
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # silence default logging; we log ourselves

def main():
    simmeta.banner("http_update_server", VERSION, FINGERPRINT,
                   ["HTTP_PORT=%s HTTP_BIND=%s LOGIN_HOST=%s CLIENT_VERSION=%s" %
                    (HTTP_PORT, HOST, LOGIN_HOST, CLIENT_VERSION)])
    srv = ThreadingHTTPServer((HOST, HTTP_PORT), Handler)
    print("[%s] http update server listening on %s:%d" % (nowtext(), HOST, HTTP_PORT))
    print("       LOGIN_HOST=%s CLIENT_VERSION=%s" % (LOGIN_HOST, CLIENT_VERSION))
    print("       log -> %s" % _LOG)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        srv.server_close()

if __name__ == "__main__":
    if "--build-webroot" in sys.argv:
        wdir = os.path.join(BASE, "webroot")
        os.makedirs(wdir, exist_ok=True)
        for name, b in (("server3.txt", build_serverlist_bytes()),
                        ("patch_list1.txt", build_patchlist_bytes()),
                        ("note.txt", b"\n")):
            with open(os.path.join(wdir, name), "wb") as f:
                f.write(b)
            print("wrote", os.path.join(wdir, name), len(b), "bytes")
    else:
        main()
