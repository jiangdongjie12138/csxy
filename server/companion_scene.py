"""World Avatars using the original client's team path-follow motors."""
import struct
from types import SimpleNamespace

from csxy_protocol import variable_message
from entity_schema import avatar_other_properties
from entity_methods import avatar_property_message


def create_avatar(session, eid, dbid, actor, team):
    from equipment_service import aspect_info
    properties = avatar_other_properties({
        'visible': 1, 'roleName': bytes.fromhex(actor['name']),
        'roleDBID': dbid, 'clevel': actor['level'], 'team': team,
        'physique': dict(zip(('school', 'sex', 'facestyle', 'hairstyle', 'haircolor'),
                            bytes.fromhex(actor['physique']))),
        'aspect': aspect_info(actor),
    })
    return variable_message(9, struct.pack('<IH3f3b', eid, 1,
        *session.position, 0, 0, 0) + properties)


def prepare(session, character):
    """Encode before publishing AOI state or committing recruitment changes."""
    from team_service import roster, property_value
    from companion_service import collection
    old = getattr(session, 'visible_companions', {})
    battle = (character or {}).get('world_state', {}).get('tutorial_battle', {})
    active = battle.get('phase') in ('preparing', 'input', 'animating')
    wanted, packets = {}, []
    if (character and getattr(session, 'world_ready', False) and not active
            and len(getattr(session, 'position', [])) == 3):
        owned = collection(character)['owned']
        for index, (key, eid, dbid, actor) in enumerate(roster(session, character)[1:], 2):
            team = dict(property_value(session, character), pos=index)
            identity = (owned[key]['id'], actor['level'], actor['physique'], actor['aspect'])
            wanted[eid] = {'identity': identity, 'team': team}
            previous = old.get(eid)
            if not previous or previous['identity'] != identity:
                if previous:
                    packets.append(variable_message(14, struct.pack('<I', eid)))
                # Spawn on a position already traversed by the player. The
                # native Follow motor uses the leader's path including height;
                # no guessed off-path height or recurring teleport is needed.
                packets.append(b'\x0b' + struct.pack('<IB', eid, 255)
                    + create_avatar(session, eid, dbid, actor, team))
            elif previous['team'] != team:
                packets.append(avatar_property_message(SimpleNamespace(entity_id=eid), 'team', team))
    packets.extend(variable_message(14, struct.pack('<I', eid)) for eid in old.keys() - wanted.keys())
    return packets, wanted


def apply(session, plan):
    packets, wanted = plan
    session.visible_companions = wanted
    return packets


def update_interest(session, character):
    return apply(session, prepare(session, character))


def hide(session):
    packets = [variable_message(14, struct.pack('<I', eid))
               for eid in getattr(session, 'visible_companions', {})]
    session.visible_companions = {}
    return packets


def entity_update(session, eid):
    """Answer the native engine's requestEntityUpdate for visible teammates."""
    from battle_service import selected
    from team_service import roster, property_value
    from companion_service import ready
    if eid not in getattr(session, 'visible_companions', {}):
        return []
    character = selected(session)
    if not character or not ready(session, character):
        return []
    for index, (_, entity, dbid, actor) in enumerate(roster(session, character)[1:], 2):
        if entity == eid:
            return [create_avatar(session, eid, dbid, actor,
                                  dict(property_value(session, character), pos=index))]
    return []
