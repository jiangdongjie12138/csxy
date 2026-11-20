# mercury.py - transport + message framing for the login server.
#
# PLACEHOLDER framing. The real client uses a 28-byte packet header whose
# first byte is type=1 (recovered earlier, not verified against a live
# capture). The exact field layout, serial handling and Mercury FIXED/VAR
# conventions for CSXY were lost with the memory dump, so the layout below is a
# self-consistent stand-in. Every constant is named so it can be swapped once
# real bytes are captured in the VM.
#
import struct


class FramingError(Exception):
    pass


# Packet header layout (placeholder): 28 bytes, first byte = type 1.
PACKET_TYPE_LOGIN = 1
_HEADER_FMT = ">BI4sIII"         # type(u8)+flags(u8)+reserved(4s)+serial(u32)+plen(u32)+pad(u32)
_RESERVED_TAIL = bytes(10)
# Header length is derived from the actual struct size so encode and decode
# always agree; never hard-code this value (struct size is easy to miscount).
HEADER_LEN = struct.calcsize(_HEADER_FMT) + len(_RESERVED_TAIL)

# Message header inside a packet.
_MSG_FMT = ">HB"                 # msgid(u16) + flags(u8)
_MSG_FLAG_VAR = 0x80


def _msg_is_var(msgid):
    # placeholder: VAR set is in messages.py
    from messages import VAR_MSGS
    return msgid in VAR_MSGS


def encode_packet(serial, msgs, reserved=b"", type_=PACKET_TYPE_LOGIN):
    """msgs: list of (msgid:int, body:bytes). Returns full packet bytes."""
    payload = b""
    for msgid, body in msgs:
        var = _msg_is_var(msgid)
        flags = _MSG_FLAG_VAR if var else 0
        payload += struct.pack(_MSG_FMT, msgid, flags)
        if var:
            payload += struct.pack(">H", len(body))
        payload += body
    if len(reserved) > 4:
        raise FramingError("reserved field too long")
    head = struct.pack(_HEADER_FMT, type_, 0, reserved, serial, len(payload), 0)
    return head + _RESERVED_TAIL + payload


def decode_packet(data):
    """Decode one packet. Returns (header_dict, list[(msgid, body)]).
    Raises FramingError on malformed data; returns None if incomplete."""
    if len(data) < HEADER_LEN:
        return None
    type_, flags, reserved, serial, plen, pad = struct.unpack_from(_HEADER_FMT, data, 0)
    if len(data) < HEADER_LEN + plen:
        return None
    payload = data[HEADER_LEN:HEADER_LEN + plen]
    msgs = []
    off = 0
    while off < len(payload):
        msgid, mflags = struct.unpack_from(_MSG_FMT, payload, off)
        off += 3
        var = bool(mflags & _MSG_FLAG_VAR)
        if var:
            (blen,) = struct.unpack_from(">H", payload, off)
            off += 2
        else:
            blen = len(payload) - off
        msgs.append((msgid, payload[off:off + blen]))
        off += blen
    return {"type": type_, "flags": flags, "reserved": reserved,
            "serial": serial, "plen": plen}, msgs
