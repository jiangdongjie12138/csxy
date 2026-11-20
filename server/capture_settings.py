"""Optional packet capture; live launcher configuration is cached for one second."""
import json
import logging
from pathlib import Path
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'launcher/launcher.json'
OUT = ROOT / 'server/evidence'
LOG = logging.getLogger('evidence')
_LOCK = threading.Lock()
_until = 0.0
_enabled = False


def enabled():
    global _until, _enabled
    now = time.monotonic()
    if now < _until:
        return _enabled
    with _LOCK:
        if now >= _until:
            try:
                _enabled = json.loads(CONFIG.read_text('utf-8-sig')).get('captureEnabled') is True
            except (OSError, ValueError, TypeError, AttributeError):
                _enabled = False
            _until = now + 1.0
        return _enabled


def write_packet(path, data):
    if not enabled():
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return True
    except (OSError, MemoryError):
        LOG.exception('Cannot save packet capture: %s', path)
        return False
