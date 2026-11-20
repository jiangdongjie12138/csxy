# sock_watch.py - high-frequency snapshot of the client process's UDP/TCP
# sockets during a login click, to verify socket lifecycle (persistent vs
# transient) and any connect() target.
import datetime, subprocess, sys, time

PID = int(sys.argv[1]) if len(sys.argv) > 1 else None
DUR = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
IV = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1


def snap(pid):
    out = []
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-Command',
            'Get-NetUDPEndpoint -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -eq %d } | ForEach-Object { \"UDP {0}:{1} -> {2}:{3}\" -f $_.LocalAddress,$_.LocalPort,$_.RemoteAddress,$_.RemotePort }; Get-NetTCPConnection -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -eq %d } | ForEach-Object { \"TCP {0}:{1} -> {2}:{3} {4}\" -f $_.LocalAddress,$_.LocalPort,$_.RemoteAddress,$_.RemotePort,$_.State }' % (pid, pid)],
            capture_output=True, text=True, timeout=5)
        out = [l for l in r.stdout.splitlines() if l.strip()]
    except Exception as e:
        out = ['ERR %s' % e]
    return out


def main():
    print('watching pid=%s dur=%.1fs iv=%.2fs' % (PID, DUR, IV), flush=True)
    t0 = time.time()
    seen = {}
    while time.time() - t0 < DUR:
        lines = snap(PID) if PID else []
        ts = time.time() - t0
        for l in lines:
            seen.setdefault(l, []).append(round(ts, 1))
        # also detect process exit
        if PID:
            r = subprocess.run(['powershell', '-NoProfile', '-Command', 'Get-Process -Id %d -ErrorAction SilentlyContinue' % PID],
                               capture_output=True, text=True, timeout=5)
            if not r.stdout.strip():
                print('[%.1f] CLIENT EXITED' % ts, flush=True)
                break
        time.sleep(IV)
    print('--- summary (first-seen: duration) ---', flush=True)
    for l, ts_list in sorted(seen.items()):
        print('  %-50s first=%.1fs last=%.1fs n=%d' % (l, ts_list[0], ts_list[-1], len(ts_list)), flush=True)


if __name__ == '__main__':
    main()