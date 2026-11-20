"""Wire format verified against the CSXY executable (protocol version 43)."""
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import copy
import json
import secrets
import socket
import struct
import threading
import time

from Crypto.Cipher import Blowfish, PKCS1_OAEP
from Crypto.PublicKey import RSA

ROOT = Path(__file__).resolve().parent.parent
KEY_DIR = ROOT / 'server' / 'keys'
if not (KEY_DIR / 'private_components.json').is_file():
    KEY_DIR = ROOT.parent / 'CSXY_work' / 'analysis' / 'rsa_test'
LOGIN_RECORD_SIZE = 20  # onLoginReply 0x502daa: retrieve(0x14)


@lru_cache(maxsize=1)
def private_key():
    c = json.loads((KEY_DIR / 'private_components.json').read_text('utf-8'))
    return RSA.construct(tuple(int.from_bytes(bytes.fromhex(c[k]), 'big')
                               for k in ('n', 'e', 'd', 'p', 'q')))


def check_resource_key():
    bag = (ROOT / 'res' / 'entities.bag').read_bytes()
    start = bag.find(b'-----BEGIN PUBLIC KEY-----')
    end = bag.find(b'-----END PUBLIC KEY-----', start)
    if start < 0 or end < 0:
        raise ValueError('entities.bag has no LoginApp public key')
    pub = RSA.import_key(bag[start:end + len(b'-----END PUBLIC KEY-----')])
    key = private_key()
    if (pub.n, pub.e) != (key.n, key.e):
        raise ValueError('LoginApp public key does not match the local private key')


def packed_bytes(data, offset):
    if offset >= len(data):
        raise ValueError('missing packed string length')
    size = data[offset]
    offset += 1
    if size == 255:
        if offset + 3 > len(data):
            raise ValueError('short packed string length')
        size = int.from_bytes(data[offset:offset + 3], 'little')
        offset += 3
    if offset + size > len(data):
        raise ValueError('packed string exceeds input')
    return data[offset:offset + size], offset + size


def login_params(datagram):
    if (len(datagram) != 145 or datagram[:5] != b'\x01\x00\x00\x84\x00'
            or datagram[9:15] != b'\x00\x00\x2b\x00\x00\x00'
            or datagram[-2:] != b'\x02\x00'):
        raise ValueError('unsupported LoginApp request format')
    plain = PKCS1_OAEP.new(private_key()).decrypt(datagram[15:143])
    offset = 1
    fields = []
    for _ in range(3):
        field, offset = packed_bytes(plain, offset)
        fields.append(field)
    if len(plain) - offset != 20:
        raise ValueError('LogOnParams must end with digest(16) and nonce(4)')
    key = fields[2]
    if not 4 <= len(key) <= 56:
        raise ValueError('invalid Blowfish key length')
    return fields[0], key


def login_key(datagram):
    return login_params(datagram)[1]


def pack_bytes(data):
    size = len(data)
    if size >= 0xffffff:
        raise ValueError('packed string is too large')
    return (bytes([size]) if size < 255 else b'\xff' + size.to_bytes(3, 'little')) + data


def variable_message(message_id, payload):
    if len(payload) >= 65535:
        raise ValueError('large variable messages require fragmentation')
    return struct.pack('<BH', message_id, len(payload)) + payload


def channel_packet(body=b'', sequence=None, acks=()):
    flags = 0x0008
    footer = b''
    if sequence is not None:
        flags |= 0x0050
        footer += struct.pack('<I', sequence)
    if acks:
        if len(acks) > 255:
            raise ValueError('too many packet acknowledgements')
        flags |= 4
        footer += b''.join(struct.pack('<I', ack) for ack in reversed(acks)) + bytes([len(acks)])
    return struct.pack('<H', flags) + body + footer


def encrypt_stream(key, data):
    clear = data + bytes(-len(data) % 8)
    cipher = Blowfish.new(key, Blowfish.MODE_ECB)
    previous = bytes(8)
    out = bytearray()
    for off in range(0, len(clear), 8):
        block = clear[off:off + 8]
        out.extend(cipher.encrypt(bytes(a ^ b for a, b in zip(block, previous))))
        previous = block
    return bytes(out)


def decrypt_stream(key, data):
    if not data or len(data) % 8:
        raise ValueError('ciphertext must be a nonempty multiple of 8')
    cipher = Blowfish.new(key, Blowfish.MODE_ECB)
    previous = bytes(8)
    out = bytearray()
    for off in range(0, len(data), 8):
        block = bytes(a ^ b for a, b in zip(cipher.decrypt(data[off:off + 8]), previous))
        out.extend(block)
        previous = block
    return bytes(out)


def encrypt_packet(key, data):
    # CSXY 0xc227a0 validates only the last-byte wastage count. The newer
    # BigWorld 2.0 ENCRYPTION_MAGIC footer is absent in this executable.
    wastage = 8 - len(data) % 8
    return encrypt_stream(key, data + bytes(wastage - 1) + bytes([wastage]))


def decrypt_packet(key, data):
    clear = decrypt_stream(key, data)
    wastage = clear[-1]
    if not 1 <= wastage <= 8 or wastage > len(clear):
        raise ValueError('invalid encrypted packet padding')
    return clear[:-wastage]


def login_record(host, port, session_key):
    # Two final words are custom CSXY fields. Their meaning is still unknown;
    # no read of them was found in the LoginHandler/BaseAppLoginRequest path.
    record = socket.inet_aton(host) + struct.pack('!H', port)
    record += struct.pack('<HIII', 0, session_key, 0, 0)
    assert len(record) == LOGIN_RECORD_SIZE
    return record


def reply_bundle(reply_id, payload, flags=0):
    if len(reply_id) != 4:
        raise ValueError('reply ID must be four raw request bytes')
    body = reply_id + payload
    return struct.pack('<HBI', flags, 255, len(body)) + body


def packet_parts(data):
    """Strip supported Mercury footers in the executable's little-endian order."""
    if len(data) < 2:
        raise ValueError('short Mercury packet')
    flags = struct.unpack_from('<H', data)[0]
    if flags & ~0x07ff or flags & 0x0080:
        raise ValueError('unsupported indexed channel packet')
    end = len(data)

    def take(size):
        nonlocal end
        if end - size < 2:
            raise ValueError('packet footer exceeds input')
        end -= size
        return data[end:end + size]

    if flags & 0x0100:
        raise ValueError('checksum packets are not implemented')
    piggybacks = []
    if flags & 0x0002:
        while True:
            length = struct.unpack('<h', take(2))[0]
            last = length < 0
            if last:
                length = (~length) & 0xffff
            if length < 6:
                raise ValueError('invalid piggyback length')
            piggybacks.append(packet_parts(take(length)))
            if last:
                break
    cumulative_ack = struct.unpack('<I', take(4))[0] if flags & 0x0400 else None
    acks = []
    if flags & 4:
        count = take(1)[0]
        if count == 0:
            raise ValueError('zero ACK count')
        acks = [struct.unpack('<I', take(4))[0] for _ in range(count)]
    sequence = struct.unpack('<I', take(4))[0] if flags & 0x0040 else None
    request_offset = struct.unpack('<H', take(2))[0] if flags & 1 else None
    fragment = None
    if flags & 0x0020:
        finish = struct.unpack('<I', take(4))[0]
        begin = struct.unpack('<I', take(4))[0]
        if sequence is None or not begin <= sequence <= finish or not 2 <= finish - begin + 1 <= 128:
            raise ValueError('invalid fragment range')
        fragment = (begin, finish)
    return {'flags': flags, 'body': data[2:end], 'sequence': sequence,
            'acks': acks, 'cumulative_ack': cumulative_ack,
            'request_offset': request_offset, 'piggybacks': piggybacks,
            'fragment': fragment}


@dataclass
class Session:
    key: bytes
    login_id: int
    peer_ip: str
    created: float
    base_peer: tuple | None = None
    next_sequence: int = 0
    username: bytes = b'local'
    entity_id: int = 1001
    account_created: bool = False
    account_logged_in: bool = False
    last_in_sequence: int = -1
    pending: dict = field(default_factory=dict)
    selected_character: int = 0
    world_started: bool = False
    position: list = field(default_factory=list)
    world_ready: bool = False
    visible_npcs: list = field(default_factory=list)
    visible_companions: dict = field(default_factory=dict)
    dungeon_switch_states: dict = field(default_factory=dict)
    dungeon_ui_id: int = 0
    battle_id: str = ''
    battle_due: float = 0
    tour_running: bool = False
    tour_due: float = 0
    npc_dialog: dict | None = None
    space_no: int | None = None
    # Logical quest maps and native engine spaces have separate lifetimes.
    # Persist this counter so restoring a channel does not reuse its old space.
    engine_space_id: int = 0
    scene_messages: list = field(default_factory=list)
    viewport_id: int = 0
    scene_transfer: dict = field(default_factory=dict)
    # Retain the login shortcut until skill and item data are available. This
    # also survives a service restart while the client is loading the scene.
    shortcut_restore_pending: str = ''
    superseded: bool = False
    # Runtime-only task helper NPC state.  Older session snapshots do not
    # contain this key, but newer world initialisation may attach it before a
    # session save.  Keeping it on the dataclass makes both old and new
    # snapshots restore safely.
    quest_guide: dict | None = None


class Sessions:
    def __init__(self):
        self.lock = threading.RLock()
        self.items = {}
        self.path = ROOT / 'server' / 'state' / 'sessions.json'
        if self.path.exists():
            for record in json.loads(self.path.read_text('utf-8')):
                record['key'] = bytes.fromhex(record['key'])
                record['username'] = bytes.fromhex(record['username'])
                # JSON object keys and tuple fingerprints need their original
                # types back when resuming an already-loaded client channel.
                record['visible_companions'] = {
                    int(eid): dict(value, identity=tuple(value['identity']))
                    for eid, value in record.get('visible_companions', {}).items()}
                if record['base_peer']:
                    record['base_peer'] = tuple(record['base_peer'])
                session = Session(**record)
                self.items[session.login_id] = session
            # Older builds retained every login. A resumed channel may own a
            # character only if a newer world login has not already taken it.
            owned = set()
            for session in sorted(self.items.values(), key=lambda item: item.created, reverse=True):
                if session.superseded or not session.world_started or not session.selected_character:
                    continue
                identity = (session.username, session.selected_character)
                if identity in owned:
                    self._retire(session)
                else:
                    owned.add(identity)

    @staticmethod
    def _retire(session):
        session.superseded = True
        session.world_started = False
        session.world_ready = False
        session.battle_id = ''
        session.battle_due = 0
        session.tour_running = False
        session.tour_due = 0
        session.npc_dialog = None
        session.scene_messages = []
        session.shortcut_restore_pending = ''
        session.pending.clear()

    def claim_character(self, session, character_id):
        """A validated character selection replaces only that character's channel."""
        with self.lock:
            if self.items.get(session.login_id) is not session or session.superseded:
                return False
            affected = [other for other in self.items.values()
                        if other is session or (other.username == session.username
                                                and other.selected_character == character_id)]
            previous = [(other, copy.deepcopy(vars(other))) for other in affected]
            for other in self.items.values():
                if (other is not session and other.username == session.username
                        and other.selected_character == character_id):
                    self._retire(other)
            session.selected_character = character_id
            try:
                self.save()
            except OSError:
                # A failed session commit must leave the existing channel usable.
                for other, fields in previous:
                    vars(other).clear()
                    vars(other).update(fields)
                raise
        return True

    def save(self):
        with self.lock:
            records = []
            for session in self.items.values():
                # These helpers are rebuilt during world initialisation and
                # belong to the live channel only.  Do not persist them in
                # the session journal (the journal can outlive a client and
                # must remain compatible with older Session snapshots).
                record = {key: value for key, value in vars(session).items()
                          if key not in ('quest_guide', '_quest_completion_cache')}
                record['key'] = session.key.hex()
                record['username'] = session.username.hex()
                records.append(record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(records, indent=2), 'utf-8')
            temp.replace(self.path)

    def register(self, key, peer_ip, username=b'local'):
        with self.lock:
            now = time.time()
            self.items = {k: v for k, v in self.items.items() if now - v.created < 86400}
            for session in self.items.values():
                if (not session.superseded and session.key == key
                        and session.peer_ip == peer_ip and session.username == username):
                    return session
            login_id = secrets.randbits(32) or 1
            while login_id in self.items:
                login_id = secrets.randbits(32) or 1
            session = Session(key, login_id, peer_ip, now, username=username)
            self.items[login_id] = session
            return session

    def candidates(self, peer):
        with self.lock:
            return sorted((s for s in self.items.values() if s.peer_ip == peer[0] and not s.superseded),
                          key=lambda s: (s.base_peer == peer, s.created), reverse=True)


SESSIONS = Sessions()
