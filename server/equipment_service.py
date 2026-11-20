"""Original equipment slots, appearance, and transactional client updates."""
import logging
import struct

import character_store
from entity_methods import avatar_client_message, avatar_property_message
from inventory_service import FIRST_BAG_PAGE, BAG_SIZE, intro_reward

LOG = logging.getLogger('equipment')
RES_KIND_EQUIPMENT = 2
WEAPON_PART = 3
EQUIPMENT_SIZE = 15
RING_PARTS = (10, 11, 12, 13)
MODEL_PARTS = range(9)
ASPECT_FIELDS = ('head', 'shoulder', 'body', 'righthand', 'lefthand', 'wrist',
                 'waist', 'leg', 'feet', 'mantle', 'primary', 'horse',
                 'shoulder_effect', 'righthand_effect', 'wrist_effect', 'aspect_hide')


def aspect_info(character):
    return dict(zip(ASPECT_FIELDS, struct.unpack('<12I3bB', bytes.fromhex(character['aspect']))))


def has_intro_weapon(character):
    item = character['equipment'].get(str(WEAPON_PART))
    return (item is not None and str(item['id']) in intro_reward()['equipment']
            and intro_reward()['items'][str(item['id'])]['stype'] == WEAPON_PART)


def validate_equipment(character, item, part):
    template = intro_reward()['equipment'].get(str(item['id']))
    base = intro_reward()['items'].get(str(item['id']))
    subtype = base.get('stype') if base else None
    slots = RING_PARTS if subtype == 10 else (14,) if subtype == 11 else (subtype,)
    if (template is None or base is None or base.get('type') != 0
            or part not in range(EQUIPMENT_SIZE) or part not in slots):
        raise ValueError('item is not restored equipment for this slot')
    if item['amount'] != 1:
        raise ValueError('equipment amount must be one')
    school, sex = bytes.fromhex(character['physique'])[:2]
    requirements = template.get('req', {})
    if (requirements.get('school') and school not in requirements['school']
            or requirements.get('sex') and sex not in requirements['sex']
            or character['level'] < base.get('req_minlv', 0)):
        raise ValueError('equipment school, sex or level requirement is not met')
    return template


def exchange(session, page, pos, part):
    if not session.world_ready or getattr(session,'battle_id',''):
        return []
    if page != FIRST_BAG_PAGE or not 0 <= pos < BAG_SIZE or part not in range(EQUIPMENT_SIZE):
        LOG.info('Equipment exchange rejected: unopened bag, invalid slot or unsupported part')
        return []
    operation = []
    objective_changed = []
    responses = []

    def change(character):
        inventory = character['inventory']
        source = next((item for item in inventory if (item['page'], item['pos']) == (page, pos)), None)
        equipped = character['equipment'].get(str(part))
        if source is None and equipped is None:
            return
        template = validate_equipment(character, source, part) if source else None
        if equipped is not None:
            del character['equipment'][str(part)]
            equipped.update(page=page, pos=pos)
        if source is not None:
            inventory.remove(source)
            source.update(page=0, pos=part)
            character['equipment'][str(part)] = source
        if equipped is not None:
            inventory.append(equipped)
        if source and equipped:
            operation.append(('res_exchange', 0, page, pos, RES_KIND_EQUIPMENT, 0, part))
        elif source:
            operation.append(('res_move_whole', 0, page, pos, RES_KIND_EQUIPMENT, 0, part))
        else:
            operation.append(('res_move_whole', RES_KIND_EQUIPMENT, 0, part, 0, page, pos))
        # Neck/rings/soulguard slots do not index ASPECT_INFO. Its fields 9+
        # describe mantle, primary, horse and effects rather than jewellery.
        if part in MODEL_PARTS:
            aspect = aspect_info(character)
            aspect[ASPECT_FIELDS[part]] = template['model'] if template else 0
            character['aspect'] = struct.pack('<12I3bB', *(aspect[name] for name in ASPECT_FIELDS)).hex()
        if source is not None and part == WEAPON_PART:
            from quest_service import mark_weapon_objective
            if mark_weapon_objective(character):
                objective_changed.append(True)
        # Construct/validate every reply before committing. A calculation or
        # serialization failure must never save a move without its reply.
        name, *arguments = operation[0]
        responses.append(avatar_client_message(session, name, *arguments))
        from mount_service import visible_aspect
        responses.append(avatar_property_message(session, 'aspect', visible_aspect(character)))
        if objective_changed:
            from quest_service import weapon_objective_message
            responses.append(weapon_objective_message(session, character))
        from attribute_service import message
        responses.append(message(session, character))

    try:
        character = character_store.transact(session, change)
    except ValueError as error:
        LOG.info('Equipment exchange rejected: %s', error)
        return []
    if character is None or not operation:
        return []
    LOG.info('Equipment exchange and objective saved: part=%d equipped=%s',
             part, str(part) in character['equipment'])
    return responses


def discard(session, part):
    """Discard an equipped item after the client's confirmation dialog."""
    if not session.world_ready or getattr(session, 'battle_id', ''):
        return []
    part = int(part)
    if part not in range(EQUIPMENT_SIZE):
        return []
    operation = []

    def change(character):
        item = character['equipment'].get(str(part))
        if item is None:
            return
        template = intro_reward()['items'].get(str(item.get('id')))
        from inventory_service import removal_error
        error = removal_error(item, template)
        if error:
            raise ValueError(error)
        del character['equipment'][str(part)]
        operation.append(avatar_client_message(session, 'res_remove', RES_KIND_EQUIPMENT, 0, part))
        if part in MODEL_PARTS:
            aspect = aspect_info(character)
            aspect[ASPECT_FIELDS[part]] = 0
            character['aspect'] = struct.pack('<12I3bB', *(aspect[name] for name in ASPECT_FIELDS)).hex()
        from attribute_service import message
        operation.append(message(session, character))
        from mount_service import visible_aspect
        operation.append(avatar_property_message(session, 'aspect', visible_aspect(character)))

    try:
        character_store.transact(session, change)
    except ValueError as error:
        from attribute_service import notice
        return [notice(session, str(error))]
    if not operation:
        return []
    return operation
