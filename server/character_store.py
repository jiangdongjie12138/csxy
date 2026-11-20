"""Local character persistence for the simulator."""
import copy
import datetime
import hashlib
import hmac
import json
import logging
import math
import os
import struct
import tempfile
import threading
import uuid

from csxy_protocol import ROOT, packed_bytes

PATH = ROOT / 'server' / 'state' / 'characters.json'
_LOCK = threading.RLock()
LOG = logging.getLogger('characters')
SCHEMA_VERSION = 1
STORE_FORMAT = 'csxy-character-store'

# These fields are deliberately plain JSON values.  The simulator can add
# task, inventory, and other world fields without changing the character-list
# wire format; ``detail`` only serializes the fields the client expects there.
DEFAULT_POSITION = [0.0, 10.0, 0.0]


class CorruptStoreError(ValueError):
    """Neither a fresh account nor an empty save may replace damaged state."""


class UnsupportedVersionError(ValueError):
    """Newer state must not be silently replaced with an older backup."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='microseconds')


def _backup_path():
    return PATH.with_suffix(PATH.suffix + '.bak')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def _normalise(character):
    """Migrate old character records and keep malformed state harmless."""
    character.setdefault('aspect', bytes(52).hex())
    character.setdefault('level', 1)
    position = character.get('position')
    if not isinstance(position, list) or len(position) != 3:
        character['position'] = list(DEFAULT_POSITION)
    else:
        try:
            position = [float(value) for value in position]
        except (TypeError, ValueError):
            position = list(DEFAULT_POSITION)
        if not all(math.isfinite(value) for value in position):
            position = list(DEFAULT_POSITION)
        character['position'] = position
    character.setdefault('space_no', None)
    character.setdefault('tasks', {})
    character.setdefault('inventory', [])
    character.setdefault('world_state', {})
    character.setdefault('equipment', {})
    character.setdefault('currency', {'cash': 0, 'binded_cash': 0, 'tongbao': 0,
                                      'yuanbao': {'pay': 0, 'free': 0}})
    character.setdefault('experience', 0)
    character.setdefault('combat_attr', {})
    character.setdefault('client_settings', {})
    character.setdefault('shortcut', '')
    character['schema_version'] = SCHEMA_VERSION
    character.setdefault('updated_at', None)
    return character


def _validate(state):
    if not isinstance(state, dict):
        raise CorruptStoreError('character account table must be an object')
    identifiers = set()
    for account, items in state.items():
        if not isinstance(account, str) or not isinstance(items, list) or len(items) > 5:
            raise CorruptStoreError('invalid character account or slot list')
        try:
            bytes.fromhex(account)
        except ValueError:
            raise CorruptStoreError('invalid character account identifier') from None
        for character in items:
            if not isinstance(character, dict):
                raise CorruptStoreError('character record must be an object')
            version = character.get('schema_version', 0)
            if type(version) is not int or version < 0:
                raise CorruptStoreError('invalid character schema version')
            if version > SCHEMA_VERSION:
                raise UnsupportedVersionError('character schema is newer than this server')
            identifier = character.get('dbid')
            if (type(identifier) is not int or not 0 < identifier < 2 ** 63
                    or identifier in identifiers):
                raise CorruptStoreError('invalid or duplicate character ID')
            identifiers.add(identifier)
            try:
                name = bytes.fromhex(character['name'])
                physique = bytes.fromhex(character['physique'])
                aspect = bytes.fromhex(character.get('aspect', bytes(52).hex()))
            except (KeyError, TypeError, ValueError):
                raise CorruptStoreError('invalid character appearance data') from None
            if not name or len(physique) != 5 or len(aspect) != 52:
                raise CorruptStoreError('invalid character appearance lengths')
            level = character.get('level', 1)
            if type(level) is not int or not 1 <= level <= 255:
                raise CorruptStoreError('invalid character level')
            for field, shape in (('tasks', dict), ('inventory', list), ('world_state', dict),
                                 ('equipment', dict), ('currency', dict), ('combat_attr', dict),
                                 ('client_settings', dict)):
                if field in character and not isinstance(character[field], shape):
                    raise CorruptStoreError('invalid character field: ' + field)
            if 'shortcut' in character and not isinstance(character['shortcut'], str):
                raise CorruptStoreError('invalid character field: shortcut')
            space_no = character.get('space_no')
            if space_no is not None and (type(space_no) is not int or not 0 <= space_no < 2 ** 32):
                raise CorruptStoreError('invalid character scene number')
            experience = character.get('experience', 0)
            if type(experience) is not int or experience < 0:
                raise CorruptStoreError('invalid character experience')
    return state


def _decode(raw):
    try:
        document = json.loads(raw.decode('utf-8-sig'))
        revision = 0
        if isinstance(document, dict) and 'format' in document:
            if document.get('format') != STORE_FORMAT:
                raise CorruptStoreError('unrecognized character store format')
            version = document.get('schema_version')
            if type(version) is not int or version < 1:
                raise CorruptStoreError('invalid character store schema version')
            if version > SCHEMA_VERSION:
                raise UnsupportedVersionError('character store is newer than this server')
            checksum = document.get('checksum')
            checked = {key: value for key, value in document.items() if key != 'checksum'}
            expected = hashlib.sha256(_canonical(checked)).hexdigest()
            if not isinstance(checksum, str) or not hmac.compare_digest(checksum, expected):
                raise CorruptStoreError('character store checksum mismatch')
            revision = document.get('revision')
            if type(revision) is not int or revision < 1:
                raise CorruptStoreError('invalid character store revision')
            state = document.get('accounts')
        else:
            state = document  # Original account-keyed JSON remains readable.
        _validate(state)
        _canonical(state)  # Reject non-finite numbers anywhere in JSON state.
        for items in state.values():
            for character in items:
                _normalise(character)
        return state, revision
    except (UnicodeError, json.JSONDecodeError, TypeError, OverflowError):
        raise CorruptStoreError('character store cannot be decoded') from None
    except ValueError as error:
        if isinstance(error, (CorruptStoreError, UnsupportedVersionError)):
            raise
        raise CorruptStoreError('character store contains invalid values') from None


def _atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.',
                                         suffix='.tmp', delete=False) as stream:
            temporary = stream.name
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def _load_state():
    try:
        return _decode(PATH.read_bytes())
    except (FileNotFoundError, CorruptStoreError) as primary_error:
        try:
            raw = _backup_path().read_bytes()
            state, revision = _decode(raw)
        except (FileNotFoundError, CorruptStoreError):
            if isinstance(primary_error, FileNotFoundError) and not _backup_path().exists():
                return {}, 0
            raise CorruptStoreError('character store is damaged; no valid recovery backup') from None
        if PATH.exists():
            quarantine = PATH.with_name(PATH.name + '.corrupt.' + uuid.uuid4().hex)
            _atomic_write(quarantine, PATH.read_bytes())
        _atomic_write(PATH, raw)
        LOG.warning('Character store restored from last valid backup; revision=%d', revision)
        return state, revision


def load():
    with _LOCK:
        return _load_state()[0]


def characters(session):
    return load().get(session.username.hex(), [])


def _write(state, revision):
    _validate(state)
    document = {'format': STORE_FORMAT, 'schema_version': SCHEMA_VERSION,
                'revision': revision + 1, 'updated_at': _now(), 'accounts': state}
    document['checksum'] = hashlib.sha256(_canonical(document)).hexdigest()
    raw = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False).encode('utf-8')
    backup = _backup_path()
    if PATH.exists():
        try:
            _decode(backup.read_bytes())
        except (FileNotFoundError, CorruptStoreError):
            # Protect a legacy save before publishing its first upgraded record.
            previous = PATH.read_bytes()
            _decode(previous)
            _atomic_write(backup, previous)
    _atomic_write(PATH, raw)
    try:
        _atomic_write(backup, raw)
    except OSError:
        # The primary commit succeeded. Do not report it as failed, which could
        # cause a caller to apply the same task reward a second time.
        LOG.warning('Character store committed; recovery backup update failed')


def transact(session, change):
    """Apply a business change while holding the complete save transaction lock."""
    selected = getattr(session, 'selected_character', 0)
    if not selected:
        return None
    with _LOCK:
        if getattr(session, 'superseded', False):
            return None
        state, revision = _load_state()
        items = state.get(session.username.hex(), [])
        character = next((item for item in items if item.get('dbid') == selected), None)
        if character is None:
            return None
        _normalise(character)
        before = copy.deepcopy(character)
        change(character)
        for name in ('dbid', 'schema_version', 'updated_at'):
            if character.get(name) != before.get(name):
                raise ValueError('cannot update character identity or store metadata')
        if revision == 0 or character != before:
            character['updated_at'] = _now()
            _write(state, revision)
        return character


def update(session, **fields):
    """Persist fields for the selected character, if one is selected."""
    def change(character):
        for name, value in fields.items():
            if name in ('dbid', 'schema_version', 'updated_at'):
                raise ValueError('cannot update character identity or store metadata')
            if name == 'position':
                if not isinstance(value, (list, tuple)) or len(value) != 3:
                    raise ValueError('character position requires three coordinates')
                try:
                    value = [float(component) for component in value]
                except (TypeError, ValueError, OverflowError):
                    raise ValueError('invalid character position') from None
                if not all(math.isfinite(component) for component in value):
                    raise ValueError('invalid character position')
            character[name] = copy.deepcopy(value)
    return transact(session, change)


def create(session, payload):
    name, offset = packed_bytes(payload, 0)
    physique = payload[offset:]
    if not name or len(physique) != 5:
        raise ValueError('createCharacter needs a name and five physique bytes')
    with _LOCK:
        state, revision = _load_state()
        items = state.setdefault(session.username.hex(), [])
        for character in items:
            if character['name'] == name.hex():
                return character
        if len(items) >= 5:
            raise ValueError('all five character slots are occupied')
        dbid = max((c['dbid'] for account in state.values() for c in account), default=10000) + 1
        character = _normalise({'dbid': dbid, 'name': name.hex(), 'physique': physique.hex(),
                                'position': list(DEFAULT_POSITION), 'updated_at': _now()})
        from skill_service import reconcile as reconcile_skills
        reconcile_skills(character)
        items.append(character)
        _write(state, revision)
        return character


def flush(session):
    """Persist the last in-memory position before a normal client logout."""
    if session.world_started and len(session.position) == 3:
        return update(session, position=session.position)
    return None


def detail(character):
    from csxy_protocol import pack_bytes
    return (struct.pack('<q', character['dbid']) + pack_bytes(bytes.fromhex(character['name']))
            + bytes([character['level']]) + bytes.fromhex(character['physique'])
            + bytes.fromhex(character['aspect']))
