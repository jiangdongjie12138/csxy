"""Entity method slots and argument encoding for the recovered client build."""
import functools
import struct
import xml.etree.ElementTree as ET

from csxy_protocol import ROOT, variable_message
from entity_schema import encode

RESOURCES = ROOT / 'analysis' / 'resources'


@functools.lru_cache(maxsize=None)
def methods(definitions, domain):
    """Flatten already resolved interfaces, retaining only exposed methods."""
    result = []
    for definition in definitions:
        root = ET.parse(RESOURCES / definition).getroot()
        result.extend(method for method in root.findall(domain + '/*')
                      if domain == 'ClientMethods' or method.find('Exposed') is not None)
    return tuple(result)


def subslot_begin(count):
    # Verified in unpacked_csxy.exe at 0x68a5b4/0x68a5ce:
    # (count + 192) / 255, then 62 - number_of_subslots.
    return 62 - ((count + 192) // 255)


def decode_slot(table, slot, arguments):
    if not 0 <= slot < 64:
        raise ValueError('invalid entity method slot')
    index = slot
    begin = subslot_begin(len(table))
    if len(table) > 62 and slot >= begin:
        if not arguments:
            raise ValueError('missing entity method subslot')
        index = begin + ((slot - begin) << 8) + arguments[0]
        arguments = arguments[1:]
    if index >= len(table):
        raise ValueError('unknown exposed entity method')
    return table[index], arguments


def client_message(entity_id, definitions, name, *arguments):
    table = methods(tuple(definitions), 'ClientMethods')
    index = next((i for i, method in enumerate(table) if method.tag == name), None)
    if index is None:
        raise ValueError('unknown client method: ' + name)
    argument_types = table[index].findall('Arg')
    if len(arguments) != len(argument_types):
        raise ValueError('incorrect client method argument count: ' + name)
    prefix = b''
    slot = index
    begin = subslot_begin(len(table))
    if len(table) > 62 and index >= begin:
        slot = begin + ((index - begin) >> 8)
        prefix = bytes([(index - begin) & 255])
    if slot >= 64:
        raise ValueError('entity method table exceeds wire slot range')
    payload = struct.pack('<I', entity_id) + prefix + b''.join(
        encode(type_node, value) for type_node, value in zip(argument_types, arguments))
    return variable_message(0x80 | slot, payload)


AVATAR_CLIENT_DEFINITIONS = ('record_0118.xml', 'record_0214.xml')


def avatar_property_message(session, name, value):
    # Client runtime full indices agree with the merged definitions: aspect=20,
    # physique=19, combatAttr=31. Mercury bit 0x40 marks a property message.
    properties = []
    for number in (258, 336, 224, 118, 214):
        root = ET.parse(RESOURCES / ('record_%04d.xml' % number)).getroot()
        properties.extend(prop for prop in root.findall('Properties/*')
                          if prop.tag == 'playerName' or prop.findtext('Flags') in
                          {'BASE_AND_CLIENT', 'ALL_CLIENTS', 'OWN_CLIENT',
                           'OTHER_CLIENTS', 'CELL_PUBLIC_AND_OWN'})
    index = next((i for i, prop in enumerate(properties) if prop.tag == name), None)
    if index is None or index >= 61:
        raise ValueError('unsupported direct client property: ' + name)
    return variable_message(0xc0 | index, struct.pack('<I', session.entity_id)
                            + encode(properties[index].find('Type'), value))


def avatar_client_message(session, name, *arguments):
    # Other recovered Avatar interfaces have no client methods.
    return client_message(session.entity_id, AVATAR_CLIENT_DEFINITIONS, name, *arguments)
