"""Encode the original, recovered WarriorInfo protobuf without game imports."""
import functools
import json
import math
import struct
from csxy_protocol import ROOT


@functools.lru_cache(maxsize=1)
def catalog():
    return json.loads((ROOT / 'server/data/tutorial_battle.json').read_text('utf-8'))


def varint(value):
    if type(value) is not int or not 0 <= value <= 0xffffffff:
        raise ValueError('protobuf uint32 out of range')
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def encode_message(name, values):
    schema = catalog()['schema'][name]
    if set(values) - {field['name'] for field in schema}:
        raise ValueError('unknown protobuf field for ' + name)
    result = bytearray()
    for field in schema:
        key, kind, number = field['name'], field['type'], field['number']
        if key not in values:
            if field['label'] == 2:
                raise ValueError('missing required protobuf field: ' + key)
            continue
        value = values[key]
        entries = value if field['label'] == 3 else [value]
        for value in entries:
            if kind in (8, 13):
                wire, encoded = 0, varint(int(value) if kind == 8 else value)
            elif kind == 7:
                wire, encoded = 5, struct.pack('<I', value)
            elif kind == 2:
                if not math.isfinite(value):
                    raise ValueError('nonfinite protobuf float')
                wire, encoded = 5, struct.pack('<f', value)
            elif kind in (11, 12):
                wire = 2
                raw = encode_message(field['message'], value) if kind == 11 else value
                if not isinstance(raw, bytes):
                    raise ValueError('protobuf bytes required')
                encoded = varint(len(raw)) + raw
            else:
                raise ValueError('unsupported recovered protobuf type')
            result.extend(varint(number * 8 + wire) + encoded)
    return bytes(result)
