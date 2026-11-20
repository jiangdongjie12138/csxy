"""Serialize entity properties from the decoded client definitions."""
import ast
import io
import pickle
import struct
import xml.etree.ElementTree as ET

from csxy_protocol import ROOT, pack_bytes

RESOURCES = ROOT / 'analysis' / 'resources'
ALIASES = ET.parse(RESOURCES / 'record_0314.xml').getroot()
FORMATS = {'INT8': 'b', 'UINT8': 'B', 'INT16': 'h', 'UINT16': 'H',
           'INT32': 'i', 'UINT32': 'I', 'INT64': 'q', 'UINT64': 'Q',
           'FLOAT': 'f', 'FLOAT64': 'd'}


class _Python2Pickler(pickle._Pickler):
    """Emit Python 2 str for bytes, without Python 3's _codecs reducer.

    protocol=2 alone still writes bytes as GLOBAL _codecs.encode + REDUCE.
    The embedded client cannot load that reducer and exposes FailedUnpickle
    to the dialog script. Native BINSTRING preserves the original GBK bytes.
    """
    dispatch = pickle._Pickler.dispatch.copy()

    def save_bytes(self, value):
        if len(value) < 256:
            self.write(pickle.SHORT_BINSTRING + bytes([len(value)]) + value)
        else:
            self.write(pickle.BINSTRING + struct.pack('<I', len(value)) + value)
        self.memoize(value)

    dispatch[bytes] = save_bytes


def python2_pickle(value):
    stream = io.BytesIO()
    _Python2Pickler(stream, protocol=2, fix_imports=True).dump(value)
    return stream.getvalue()


def primitive_default(default):
    if default is None or default.text is None:
        return None
    value = default.text.strip()
    if value == 'set()':
        return set()
    if value in ('true', 'false'):
        return value == 'true'
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value


def packed_count(size):
    return bytes([size]) if size < 255 else b'\xff' + size.to_bytes(3, 'little')


def encode(type_node, value=None, default=None):
    name = (type_node.text or '').strip()
    alias = ALIASES.find(name)
    if alias is not None:
        return encode(alias, value, default if default is not None else alias.find('Default'))
    if value is None:
        value = primitive_default(default)
    if name in FORMATS:
        return struct.pack('<' + FORMATS[name], value if value is not None else 0)
    if name in ('STRING', 'BLOB'):
        if value is None:
            value = b''
        if isinstance(value, str):
            value = value.encode('gbk')
        return pack_bytes(value)
    if name == 'PYTHON':
        return pack_bytes(python2_pickle(value))
    if name in ('VECTOR2', 'VECTOR3', 'VECTOR4'):
        count = int(name[-1])
        value = value or [0.0] * count
        if isinstance(value, str):
            value = [float(x) for x in value.split()]
        return struct.pack('<' + 'f' * count, *value)
    if name in ('ARRAY', 'TUPLE'):
        size = type_node.findtext('size')
        values = value or []
        if size:
            values = values or [None] * int(size)
            if len(values) != int(size):
                raise ValueError('incorrect fixed array length')
        # CSXY's ARRAY uses uint32 length, unlike the later engine's packed
        # count. reportException(ARRAY<BLOB>) provides an independent capture.
        return (b'' if size else struct.pack('<I', len(values))) + b''.join(
            encode(type_node.find('of'), x) for x in values)
    if name == 'FIXED_DICT':
        values = value if isinstance(value, dict) else {}
        prefix = b''
        if type_node.findtext('AllowNone', 'false').strip().lower() == 'true':
            prefix = b'\x01'
        result = bytearray(prefix)
        for prop in type_node.findall('Properties/*'):
            prop_default = default.find(prop.tag) if default is not None else None
            if prop_default is None:
                prop_default = prop.find('Default')
            result.extend(encode(prop.find('Type'), values.get(prop.tag), prop_default))
        return bytes(result)
    raise ValueError('unsupported entity data type: ' + name)


def avatar_properties(domain, overrides):
    roots = [ET.parse(RESOURCES / ('record_%04d.xml' % n)).getroot()
             for n in (258, 336, 224, 118, 214)]
    accepted = ({'BASE_AND_CLIENT'} if domain == 'base' else
                {'ALL_CLIENTS', 'OWN_CLIENT', 'OTHER_CLIENTS', 'CELL_PUBLIC_AND_OWN'})
    data = bytearray()
    offsets = []
    for root in roots:
        for prop in root.findall('Properties/*'):
            # The running CSXY Avatar inherits iProxyNoCell.playerName as
            # BASE_AND_CLIENT (runtime flags 0xac, client property index 0).
            # The archived iProxy definition labels it BASE. Omitting its
            # packed BLOB shifts every later property, producing FailedUnpickle.
            inherited_player_name = domain == 'base' and prop.tag == 'playerName'
            if prop.findtext('Flags') not in accepted and not inherited_player_name:
                continue
            encoded = encode(prop.find('Type'), overrides.get(prop.tag), prop.find('Default'))
            offsets.append((prop.tag, len(data), len(encoded)))
            data.extend(encoded)
    return bytes(data), offsets


def npc_properties(definition, overrides):
    # Runtime NPC indices: spaceNo=0 (own only), visible=1, roleNameEx=2,
    # titleEx=3, aspect=4, fightFlag=5, concrete NPC identifier=6.
    filenames = ('record_0224.xml', 'record_0118.xml')
    if definition != 'record_0257.xml':
        filenames += ('record_0316.xml',)
    roots = [ET.parse(RESOURCES / filename).getroot() for filename in filenames + (definition,)]
    client_flags = {'BASE_AND_CLIENT', 'ALL_CLIENTS', 'OWN_CLIENT',
                    'OTHER_CLIENTS', 'CELL_PUBLIC_AND_OWN'}
    other_flags = {'ALL_CLIENTS', 'OTHER_CLIENTS'}
    entries = []
    index = 0
    for root in roots:
        for prop in root.findall('Properties/*'):
            flags = prop.findtext('Flags')
            if flags not in client_flags:
                continue
            if flags in other_flags:
                value = encode(prop.find('Type'), overrides.get(prop.tag), prop.find('Default'))
                entries.append(bytes([index]) + value)
            index += 1
    return bytes([len(entries)]) + b''.join(entries)


def avatar_other_properties(overrides):
    """Tagged public properties for createEntity, with full runtime indices."""
    entries, index = [], 0
    for number in (258, 336, 224, 118, 214):
        root = ET.parse(RESOURCES / ('record_%04d.xml' % number)).getroot()
        for prop in root.findall('Properties/*'):
            flags = prop.findtext('Flags')
            if prop.tag != 'playerName' and flags not in {
                    'BASE_AND_CLIENT', 'ALL_CLIENTS', 'OWN_CLIENT',
                    'OTHER_CLIENTS', 'CELL_PUBLIC_AND_OWN'}:
                continue
            if flags in {'ALL_CLIENTS', 'OTHER_CLIENTS'}:
                entries.append(bytes([index]) + encode(prop.find('Type'),
                    overrides.get(prop.tag), prop.find('Default')))
            index += 1
    return bytes([len(entries)]) + b''.join(entries)
