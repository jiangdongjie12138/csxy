# messages.py - CSXY message-id registry.
#
# Honesty note: the real numeric message ids for CSXY lived only in a lost
# memory dump and were never re-captured from the live client. The ids below
# are PLACEHOLDERS for the message *names* that were documented earlier. The
# mapping table is centralised here so that once real ids are recovered (by
# capturing the client in the VM), only this file needs editing.
#
from enum import IntEnum


class Msg(IntEnum):
    """Message ids used by the login session.

    Numeric values are PLACEHOLDERS (unverified). Order/naming reflects the
    documented client-side message vocabulary.
    """
    # ---- login session ----
    LOGIN          = 0x0001  # placeholder
    LOGIN_REPLY    = 0x0002  # placeholder
    # ---- avatar/character (documented names, ids unverified) ----
    CREATE_BASE_PLAYER = 0x0003  # placeholder (VARIABLE fields unresolved)
    AVATAR_INFO        = 0x0004  # placeholder
    ENTER_WORLD        = 0x0005  # placeholder
    # ---- generic (reserved for later) ----
    PING           = 0x00F0  # placeholder
    PONG           = 0x00F1  # placeholder
    ERROR          = 0x00FF  # placeholder

    @classmethod
    def name_of(cls, value):
        for m in cls:
            if m.value == value:
                return m.name
        return "UNKNOWN_0x%04X" % value


# Per-message framing: FIXED payloads have no length prefix; VAR have a u16
# length prefix before the body. Placeholder assumption until field layout is
# recovered from a real capture.
VAR_MSGS = frozenset({Msg.LOGIN, Msg.CREATE_BASE_PLAYER})
FIXED_MSGS = frozenset({Msg.LOGIN_REPLY, Msg.AVATAR_INFO, Msg.PING,
                        Msg.PONG, Msg.ERROR, Msg.ENTER_WORLD})

# Well-known account/role field layout for LOGIN body (placeholder).
# Recovered-later real layout will replace this.
LOGIN_FIELDS = ("account", "password", "version")   # placeholder
