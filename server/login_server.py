# login_server.py v0.2 - CSXY login-app simulator on TCP 20560.
#
# v0.2 changes (2026-10-06):
#   - Every raw byte received on a connection is archived to
#     evidence/login_frames/conn_*.bin (+ .json sidecar) BEFORE any parsing.
#     v0.1 dropped unparseable bytes (real client traffic never reached the
#     frame log), which made a real-capture round useless.
#   - Version banner with unique fingerprint + source sha1 self-print.
#
# Honesty: the reply path still speaks OUR placeholder Mercury framing, NOT
# the real client's bytes (verified only against mock_client). Labels:
#   PLACEHOLDER  = self-consistent stand-in, not observed from the real client
#   OBSERVED     = byte-verified against a real csxy client capture
# There are no OBSERVED branches yet. The default REPLY=on keeps the mock
# self-consistency test green; set REPLY=off for a pure capture listener.
#
import datetime, os, socket, struct, threading, sys

import mercury
from messages import Msg

VERSION = "0.2.0"
FINGERPRINT = "csxy-login-v0.2-a3f7d91c"
import simmeta

PORT = int(os.environ.get("LOGIN_PORT", "20560"))
BIND = os.environ.get("LOGIN_BIND", "0.0.0.0")
REPLY = os.environ.get("REPLY", "on").lower() in ("1", "on", "yes", "true")
BASE = os.path.dirname(os.path.abspath(__file__))
from capture_settings import OUT as CAPTURE_ROOT
EVID = str(CAPTURE_ROOT / 'login_frames')

from framelog import ConnArchive


class LoginSession:
    """Per-connection state machine (placeholder protocol, see module docstring)."""

    def __init__(self, conn, addr, serial_clock, seq):
        self.conn = conn
        self.addr = addr
        self.serial_clock = serial_clock
        self.seq = seq
        self.buf = b""
        self.state = "CONNECTED"
        self.account = None
        self.session_id = 0
        self.archive = ConnArchive(EVID, seq, addr)

    def on_bytes(self, data):
        # Evidence first: the raw bytes are archived unconditionally.
        self.archive.record_in(data)
        self.buf += data
        out = []
        while True:
            try:
                dec = mercury.decode_packet(self.buf)
            except mercury.FramingError:
                # Unknown bytes: keep them archived, stay silent, wait for
                # more / timeout. This is the real-client path until the
                # actual protocol is recovered.
                if REPLY:
                    out.append(("RAW(unparsed)", len(data)))
                break
            if dec is None:
                break
            head, msgs = dec
            self.buf = self.buf[mercury.HEADER_LEN + head["plen"]:]
            out.extend(self._handle(head, msgs))
        return out

    def _handle(self, head, msgs):
        replies = []
        for msgid, body in msgs:
            name = Msg.name_of(msgid)
            if msgid == Msg.LOGIN and self.state == "CONNECTED":
                self.account = body.decode("latin-1", "replace")
                self.state = "AUTH_OK"
                self.session_id = (self.serial_clock() & 0xFFFFFFFF)
                reply = mercury.encode_packet(
                    self.session_id,
                    [(Msg.LOGIN_REPLY, self._build_login_reply())])
                if REPLY:
                    self.archive.record_out(reply)
                    self.conn.sendall(reply)
                replies.append("login->reply(placeholder) serial=%d" % self.session_id)
            elif msgid == Msg.PING:
                reply = mercury.encode_packet(self.serial_clock(), [(Msg.PONG, b"")])
                if REPLY:
                    self.archive.record_out(reply)
                    self.conn.sendall(reply)
                replies.append("ping->pong(placeholder)")
            else:
                replies.append("ignored %s state=%s" % (name, self.state))
        return replies

    def _build_login_reply(self):
        # PLACEHOLDER reply body: session id (u32) + reserved. Unverified.
        return struct.pack(">I", self.session_id) + b"\x00" * 4

    def close(self, note=""):
        meta = self.archive.close(note=note)
        return meta


def serve(port=PORT, bind=BIND, reply=REPLY, done=None, on_ready=None):
    """Run the accept loop in the calling thread. `done` (optional Event)
    stops the loop. `on_ready(port)` is called once bound (tests use port 0).
    Returns the socket so tests can close it."""
    global REPLY
    REPLY = reply

    serial = [0]

    def next_serial():
        serial[0] += 1
        return serial[0]

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(16)
    bound_port = srv.getsockname()[1]

    if on_ready:
        on_ready(bound_port)
    print("[%s] login server listening on %s:%s  reply=%s" %
          (datetime.datetime.now().strftime("%H:%M:%S"), bind,
           "0" if port == 0 else bound_port, "on(placeholder)" if REPLY else "off(capture-only)"))
    print("     evidence -> %s" % EVID)

    seq = [0]

    def handle(conn, addr):
        seq[0] += 1
        sess = LoginSession(conn, addr, next_serial, seq[0])
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                for line in sess.on_bytes(data):
                    print("     [%s] %s" % (addr, line))
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            meta = sess.close()
            print("     [%s] closed state=%s bytes_in=%d sha1=%s" %
                  (addr, sess.state, meta["bytes_in"], meta["sha1"][:12]))

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
    simmeta.banner("login_server", VERSION, FINGERPRINT,
                   ["LOGIN_PORT=%s LOGIN_BIND=%s REPLY=%s" % (PORT, BIND, REPLY),
                    "evidence dir: %s" % EVID,
                    "PLACEHOLDER framing - no real-client protocol yet"])
    try:
        serve()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
