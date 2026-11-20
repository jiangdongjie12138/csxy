# framelog.py - evidence landing zone for B-class (network protocol) objects.
#
# Every connection to the simulated servers gets one raw binary archive file
# (all bytes exactly as received) plus a JSON sidecar closed when the
# connection ends. Raw bytes are the primary evidence; decoded interpretations
# live next to them, never instead of them.
#
import datetime, hashlib, json, os, threading
from capture_settings import enabled, LOG


class ConnArchive:
    """Per-connection raw capture appended to disk as bytes arrive."""

    def __init__(self, out_dir, seq, peer):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(out_dir, "conn_%03d_%s.bin" % (seq, ts))
        self.meta_path = self.path[:-4] + ".json"
        self.peer = peer
        self.bytes_in = 0
        self.bytes_out = 0
        self.start = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._fh = None
        self._lock = threading.Lock()

    def record_in(self, data):
        with self._lock:
            if not enabled():
                if self._fh is not None:
                    try:
                        self._fh.close()
                    except OSError:
                        LOG.exception('Cannot close TCP capture: %s', self.path)
                    self._fh = None
                return
            try:
                if self._fh is None:
                    os.makedirs(os.path.dirname(self.path), exist_ok=True)
                    self._fh = open(self.path, 'ab')
                self._fh.write(data)
                self._fh.flush()
                self.bytes_in += len(data)
            except (OSError, MemoryError):
                LOG.exception('Cannot write TCP capture: %s', self.path)

    def record_out(self, data):
        with self._lock:
            self.bytes_out += len(data)

    def close(self, note=""):
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    LOG.exception('Cannot close TCP capture: %s', self.path)
                self._fh = None
        if not enabled() or not os.path.isfile(self.path):
            return None
        try:
            with open(self.path, 'rb') as stream:
                sha = hashlib.sha1(stream.read()).hexdigest()
        except (OSError, MemoryError):
            LOG.exception('Cannot read TCP capture: %s', self.path)
            return None
        meta = {
            "file": os.path.basename(self.path),
            "peer": "%s:%d" % (self.peer[0], self.peer[1]) if self.peer else None,
            "start": self.start,
            "end": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "sha1": sha,
            "note": note,
        }
        try:
            with open(self.meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=1)
        except (OSError, MemoryError):
            LOG.exception('Cannot write TCP capture metadata: %s', self.meta_path)
            return None
        return meta
