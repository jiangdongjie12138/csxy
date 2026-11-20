# port_watch.py - snapshot every UDP socket owned by csxy.exe while user
# clicks login, to verify which local port receives the reply.
import datetime, json, os, subprocess, sys, time

OUT = r'D:\BaiduNetdiskDownload\CSXY_work\evidence\login_frames'
os.makedirs(OUT, exist_ok=True)


def netstat_udp():
    try:
        r = subprocess.run(['netstat', '-ano', '-p', 'udp'], capture_output=True,
                           text=True, timeout=5, errors='replace')
        return r.stdout
    except Exception as e:
        return 'ERR %s' % e


def snap(label):
    rows = []
    for line in netstat_udp().splitlines():
        if 'UDP' not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        # UDP  <local>  <remote>  PID
        local, remote, pid = parts[1], parts[2], parts[-1]
        if pid.isdigit() and int(pid) == CSXY_PID:
            rows.append({'local': local, 'remote': remote})
    rec = {'t': datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3], 'label': label,
           'udp_sockets': rows}
    return rec


if __name__ == '__main__':
    # find csxy pids via Get-CimInstance-free approach: tasklist exact
    r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq csxy.exe'], capture_output=True,
                       text=True, timeout=10, errors='replace')
    pids = set()
    for line in r.stdout.splitlines()[1:]:
        if 'csxy' not in line.lower():
            continue
        parts = line.split()
        # tasklist: csxy.exe  <pid> <session> <mem>
        for p in parts:
            if p.isdigit() and len(p) >= 3:
                pids.add(int(p))
                break
    if not pids:
        # fallback: netstat -ano | find csxy pid by process name via wmic
        r2 = subprocess.run(['wmic', 'process', 'where', "name='csxy.exe'", 'get', 'ProcessId'],
                            capture_output=True, text=True, timeout=10, errors='replace')
        for line in r2.stdout.splitlines():
            if line.strip().isdigit():
                pids.add(int(line.strip()))
    if not pids:
        print('no csxy running'); sys.exit(1)
    CSXY_PID = sorted(pids)[0]
    print('watching csxy pid(s) %s - snapshot every 0.5s for 60s. Ctrl+C to stop.' % sorted(pids))
    log = []
    t0 = time.time()
    i = 0
    while time.time() - t0 < 60:
        rec = snap(i)
        i += 1
        log.append(rec)
        line = ' '.join('UDP %s->%s' % (x['local'], x['remote']) for x in rec['udp_sockets'])
        print('[%s] %s: %s' % (rec['t'], rec['label'], line or '(no udp sockets)'), flush=True)
        time.sleep(0.5)
    fn = os.path.join(OUT, 'port_watch_%s.json' % datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
    with open(fn, 'w') as f:
        json.dump(log, f, indent=1)
    print('saved', fn)