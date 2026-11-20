# baseapp_server.py v0.1 - CSXY base-app (world) simulator on TCP 28000.
#
# 28000 is the world-entry ("中心玩法" / baseApp) port from server3.txt.
# v0.1 is a pure evidence listener: every byte of every connection is archived
# to evidence/baseapp_frames/conn_*.bin (+ .json sidecar). Nothing is replied
# by default (REPLY=off) so the real client's first world-handshake bytes are
# captured intact. No protocol knowledge is assumed.
#
import datetime, os, socket, threading

VERSION = "0.1.0"
FINGERPRINT = "csxy-baseapp-v0.1-1b8e52f3"
import simmeta

PORT = int(os.environ.get("BASEAPP_PORT", "28000"))
BIND = os.environ.get("BASEAPP_BIND", "0.0.0.0")
REPLY = os.environ.get("REPLY", "off").lower() in ("1", "on", "yes", "true")
BASE = os.path.dirname(os.path.abspath(__file__))
from capture_settings import OUT as CAPTURE_ROOT
EVID = str(CAPTURE_ROOT / 'baseapp_frames')

from framelog import ConnArchive


def serve(port=PORT, bind=BIND, reply=REPLY, done=None, on_ready=None):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(16)
    bound_port = srv.getsockname()[1]
    if on_ready:
        on_ready(bound_port)
    print("[%s] baseapp server listening on %s:%s  reply=%s" %
          (datetime.datetime.now().strftime("%H:%M:%S"), bind,
           "0" if port == 0 else bound_port, "on(placeholder)" if reply else "off(capture-only)"))
    print("     evidence -> %s" % EVID)

    seq = [0]

    def handle(conn, addr):
        seq[0] += 1
        arch = ConnArchive(EVID, seq[0], addr)
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                arch.record_in(data)
                print("     [%s] recv %d bytes (total %d)" % (addr, len(data), arch.bytes_in))
                if reply:
                    # PLACEHOLDER: echo nothing meaningful; keep the socket
                    # open so the client's reader blocks, still capturing.
                    pass
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            meta = arch.close()
            print("     [%s] closed bytes_in=%d sha1=%s" %
                  (addr, meta["bytes_in"], meta["sha1"][:12]))

    while done is None or not done.is_set():
        try:
            conn, addr = srv.accept()
        except OSError:
            break
        print("     [%s] accepted" % (addr,))
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()
    srv.close()
    return srv


def main():
    simmeta.banner("baseapp_server", VERSION, FINGERPRINT,
                   ["BASEAPP_PORT=%s BASEAPP_BIND=%s REPLY=%s" % (PORT, BIND, REPLY),
                    "evidence dir: %s" % EVID,
                    "capture-only listener; protocol not assumed"])
    try:
        serve()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
