# dual_listen.py - observe the client's FULL login behavior on port 20560:
# both UDP datagrams and TCP connections, capture-only (no replies).
# Goal: determine whether UDP probe is a handshake and login goes over TCP.
import datetime, json, os, socket, threading

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(BASE, '..', '..', 'evidence', 'login_frames'))
os.makedirs(OUT, exist_ok=True)
TS = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
manifest = []


def log(kind, msg):
    print('[%s] %s %s' % (datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3], kind, msg), flush=True)


def save(name, data, note=''):
    fn = '%s_%s.bin' % (name, TS)
    with open(os.path.join(OUT, fn), 'wb') as f:
        f.write(data)
    rec = {'kind': name, 'file': fn, 'size': len(data), 'time':
           datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3], 'note': note}
    manifest.append(rec)
    with open(os.path.join(OUT, 'dual_%s.manifest.json' % TS), 'w') as f:
        json.dump({'manifest': manifest}, f, indent=1)
    return fn


def udp_loop():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', 20560))
    log('UDP', 'listening on 0.0.0.0:20560 (capture-only)')
    while True:
        data, addr = s.recvfrom(65535)
        fn = save('udp_login', data, 'from %s:%d' % addr)
        log('UDP', '<- %s:%d len=%d saved=%s' % (addr[0], addr[1], len(data), fn))


def handle_tcp(conn, addr):
    conn.settimeout(8)
    buf = b''
    while True:
        try:
            chunk = conn.recv(65536)
        except socket.timeout:
            break
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    if buf:
        fn = save('tcp_login', buf, 'from %s:%d' % addr)
        log('TCP', '<- %s:%d total=%d saved=%s' % (addr[0], addr[1], len(buf), fn))
    conn.close()


def tcp_loop():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', 20560))
    s.listen(16)
    log('TCP', 'listening on 0.0.0.0:20560 (capture-only)')
    while True:
        try:
            conn, addr = s.accept()
        except OSError:
            break
        log('TCP', 'ACCEPT %s:%d' % addr)
        threading.Thread(target=handle_tcp, args=(conn, addr), daemon=True).start()


if __name__ == '__main__':
    threading.Thread(target=udp_loop, daemon=True).start()
    threading.Thread(target=tcp_loop, daemon=True).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass