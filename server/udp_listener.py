# udp_listener.py - loginserver UDP probe listener.
# Archives each datagram to its own file (udp_login_<ts>_<seq>.bin) plus a
# JSON manifest - per-datagram boundaries preserved, no file locking issue.
import datetime, json, os, socket, threading, time

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(BASE, '..', '..', 'evidence', 'login_frames'))
os.makedirs(OUT, exist_ok=True)


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', 20560))
    print('UDP login listener on 0.0.0.0:20560 ->', OUT, flush=True)
    manifest = []
    seq = 0
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    while True:
        s.settimeout(30)
        try:
            data, addr = s.recvfrom(65535)
        except socket.timeout:
            continue
        seq += 1
        stamp = datetime.datetime.now()
        fn = 'udp_login_%s_%03d.bin' % (ts, seq)
        with open(os.path.join(OUT, fn), 'wb') as f:
            f.write(data)
        rec = {'seq': seq, 'file': fn, 'size': len(data),
               'peer': '%s:%d' % addr,
               'time': stamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}
        manifest.append(rec)
        with open(os.path.join(OUT, 'udp_login_%s.manifest.json' % ts), 'w') as f:
            json.dump({'records': manifest}, f, indent=1)
        print('[%s] <- %s:%d len=%d saved=%s' %
              (stamp.strftime('%H:%M:%S.%f')[:-3], addr[0], addr[1], len(data), fn),
              flush=True)


if __name__ == '__main__':
    main()