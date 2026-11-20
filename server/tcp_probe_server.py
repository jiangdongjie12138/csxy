# tcp_probe_server.py - listen TCP 20560 AND reply UDP 20560 with a chosen
# mode, then REPORT whether the client ever opens a TCP connection.
# Decisive experiment: if UDP reply is accepted, client should attempt TCP
# (login with account/password). If never, UDP reply format is still wrong.
import datetime, json, os, socket, threading, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(BASE, '..', '..', 'evidence', 'login_frames'))
os.makedirs(OUT, exist_ok=True)
MODE = os.environ.get('REPLY_MODE', 'echo')
TCP_ATTEMPTS = []
LOCK = threading.Lock()
TS = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')


def log(kind, msg):
    print('[%s] %s %s' % (datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3], kind, msg), flush=True)


def build_reply(datagram, mode):
    if mode == 'echo':
        return datagram
    if mode == 'hdrzeros':
        return datagram[:15] + b'\x00' * 128 + datagram[143:145]
    if mode == 'zeros145':
        return b'\x00' * 145
    raise ValueError(mode)


def udp_loop():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', 20560))
    log('UDP', 'listening 0.0.0.0:20560 reply=%s' % MODE)
    while True:
        data, addr = s.recvfrom(65535)
        fn = 'udp_%s_%s.bin' % (TS, datetime.datetime.now().strftime('%H%M%S_%f')[:-3])
        with open(os.path.join(OUT, fn), 'wb') as f:
            f.write(data)
        reply = build_reply(data, MODE)
        if reply:
            s.sendto(reply, addr)
        log('UDP', '<- %s:%d len=%d saved=%s reply=%dB' % (addr[0], addr[1], len(data), fn, len(reply)))


def tcp_loop():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', 20560))
    s.listen(16)
    log('TCP', 'listening 0.0.0.0:20560')
    while True:
        try:
            conn, addr = s.accept()
        except OSError:
            break
        with LOCK:
            TCP_ATTEMPTS.append(datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3])
        log('TCP', '*** CLIENT CONNECTED from %s:%d  ***' % addr)
        conn.settimeout(15)
        buf = b''
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 65536:
                    break
        except socket.timeout:
            pass
        except OSError:
            pass
        if buf:
            fn = 'tcp_%s_%s.bin' % (TS, datetime.datetime.now().strftime('%H%M%S_%f')[:-3])
            with open(os.path.join(OUT, fn), 'wb') as f:
                f.write(buf)
            log('TCP', '  received %d bytes saved=%s  first= %s' % (len(buf), fn, buf[:32].hex()))
        conn.close()


if __name__ == '__main__':
    threading.Thread(target=udp_loop, daemon=True).start()
    threading.Thread(target=tcp_loop, daemon=True).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    log('RESULT', 'TCP attempts: %d' % len(TCP_ATTEMPTS))