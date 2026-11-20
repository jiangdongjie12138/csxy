# udp_capture.py - UDP listeners on the CSXY server ports (20560 login,
# 28000 baseapp). Archives every datagram (timestamped) to
# evidence/{login_frames,baseapp_frames}/udp_*.bin + sidecar. TCP channels are
# handled by the TCP servers; this covers the UDP hypothesis for the
# "connection timeout with zero TCP accepts" observation.
import datetime, os, socket, threading, time

BASE = os.path.dirname(os.path.abspath(__file__))
EVID_LOGIN = os.path.normpath(os.path.join(BASE, "..", "..", "evidence", "login_frames"))
EVID_BASE = os.path.normpath(os.path.join(BASE, "..", "..", "evidence", "baseapp_frames"))
os.makedirs(EVID_LOGIN, exist_ok=True)
os.makedirs(EVID_BASE, exist_ok=True)

PORTS = [("login", 20560, EVID_LOGIN), ("baseapp", 28000, EVID_BASE)]


def listen(name, port, outdir, stop):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    print("[%s] UDP %s listening on 0.0.0.0:%d -> %s" %
          (datetime.datetime.now().strftime("%H:%M:%S"), name, port, outdir), flush=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fh = open(os.path.join(outdir, "udp_%s_%s.bin" % (name, ts)), "ab")
    n = 0
    try:
        while not stop.is_set():
            s.settimeout(0.5)
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            n += 1
            stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            fh.write(data)
            fh.flush()
            print("[%s] UDP %s <- %s:%d  datagram#%d len=%d" %
                  (stamp, name, addr[0], addr[1], n, len(data)), flush=True)
    finally:
        fh.close()


def main():
    stop = threading.Event()
    threads = [threading.Thread(target=listen, args=(n, p, d, stop), daemon=True)
               for n, p, d in PORTS]
    for t in threads:
        t.start()
    print("UDP capture running. Ctrl+C to stop.", flush=True)
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        print("stopped", flush=True)


if __name__ == "__main__":
    main()