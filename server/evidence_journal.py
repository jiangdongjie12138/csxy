"""Append each capture's metadata without retaining a growing in-memory list."""
import json
import logging
from capture_settings import enabled

LOG = logging.getLogger('evidence')


def append_record(path, record):
    # JSON Lines keeps old records readable even after a service interruption.
    # Capture failures must not take the playable network listener offline.
    if not enabled():
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
    except (OSError, MemoryError):
        LOG.exception('Cannot append capture metadata: %s', path)
        return False
    return True
