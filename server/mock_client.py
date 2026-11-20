# mock_client.py - simulated CSXY client driving the login session end-to-end.
# Connects to 127.0.0.1:20560, sends a LOGIN, expects a LOGIN_REPLY, then PING.
# Self-consistency proof only; speaks the same placeholder framing as the server.
import os, socket, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mercury
from messages import Msg

HOST = os.environ.get("LOGIN_HOST", "127.0.0.1")
PORT = int(os.environ.get("LOGIN_PORT", "20560"))


def main():
    s = socket.create_connection((HOST, PORT), timeout=8)
    serial = [100]

    def nxt():
        serial[0] += 1
        return serial[0]

    print("connected to %s:%d" % (HOST, PORT))

    # LOGIN (placeholder body: account + password, VAR)
    login_body = b"test_user\x00test_pass\x001.1.179"
    s.sendall(mercury.encode_packet(nxt(), [(Msg.LOGIN, login_body)]))
    print("sent LOGIN")

    # read until a full reply packet
    buf = b""
    got_reply = False
    import time
    deadline = time.time() + 5
    while time.time() < deadline:
        d = s.recv(65536)
        if not d:
            break
        buf += d
        try:
            dec = mercury.decode_packet(buf)
        except mercury.FramingError:
            break
        if dec is not None:
            head, msgs = dec
            for msgid, body in msgs:
                print("received msg=%s body=%s" % (Msg.name_of(msgid), body.hex()))
                if msgid == Msg.LOGIN_REPLY:
                    got_reply = True
                    if len(body) >= 4:
                        sid = int.from_bytes(body[:4], "big")
                        print("  session_id=0x%08x (%d)" % (sid, sid))
            break

    # PING/PONG
    s.sendall(mercury.encode_packet(nxt(), [(Msg.PING, b"")]))
    print("sent PING")
    deadline = time.time() + 5
    while time.time() < deadline:
        d = s.recv(65536)
        if not d:
            break
        try:
            dec = mercury.decode_packet(d)
        except mercury.FramingError:
            break
        if dec is not None:
            head, msgs = dec
            for msgid, body in msgs:
                print("received msg=%s" % Msg.name_of(msgid))
            break

    s.close()
    print("RESULT:", "PASS" if got_reply else "FAIL")


if __name__ == "__main__":
    main()
