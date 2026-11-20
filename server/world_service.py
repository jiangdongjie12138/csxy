"""Initial player entity and geometry mapping for a local test world."""
import logging
import os
import struct
import time
import uuid
import zlib

from csxy_protocol import variable_message
from entity_schema import avatar_properties
from character_store import characters
from npc_service import map_config
from entity_methods import avatar_client_message, avatar_property_message

LOG = logging.getLogger('world')
SERVER_BOOT_TIME = int(time.time())


def shortcut_wire_payload(value):
    """Return the shortcut blob in the format expected by the client.

    The base ``saveShortcut`` method stores a Python-2 pickle directly.  The
    client-side ``update_shortcut`` handler first runs ``zlib.decompress`` and
    then unpickles the result, so persistence keeps the original bytes and the
    compression is applied only when constructing the outbound message.
    """
    if isinstance(value, str):
        value = bytes.fromhex(value)
    value = bytes(value)
    if len(value) > 256 * 1024:
        raise ValueError('shortcut blob is too large')
    return zlib.compress(value)


def time_message(session):
    # PlayerAvatar.server_time -> correct_time -> Quest.initQuestData; without
    # this the client has no quest indices and hides task-dependent NPCs.
    return avatar_client_message(session, 'server_time', SERVER_BOOT_TIME, int(time.time()))


def mapping_message(path=None, remove=False, space_id=1):
    path = (path or os.environ.get('WORLD_MAP', 'universes/eg/xinsd')).encode('ascii')
    payload = struct.pack('<I', space_id) + struct.pack('<IHH', 1, 0, 0)
    if remove:
        return variable_message(7, payload + struct.pack('<H', 65535))
    matrix = [1.0 if i % 5 == 0 else 0.0 for i in range(16)]
    return variable_message(7, payload + struct.pack('<H16f', 1, *matrix) + path)


def initial_messages(session):
    from battle_service import interrupt_on_login
    interrupt_on_login(session)
    from inventory_service import repair_intro_bag, repair_summon_bag
    try:
        repair_intro_bag(session)
        repair_summon_bag(session)
    except ValueError as error:
        LOG.warning('Inventory layout repair postponed: %s', error)
    from growth_service import repair as repair_growth, potential
    character = repair_growth(session)
    from skill_service import repair as repair_skills
    character = repair_skills(session)
    from summon_service import repair as repair_summon, messages as summon_messages
    character = repair_summon(session)
    physique = bytes.fromhex(character['physique'])
    from mount_service import visible_aspect
    from attribute_service import combat_attributes
    configured_space = map_config()['space_no']
    saved_space = character.get('space_no')
    try:
        space_no = int(saved_space) if saved_space is not None else configured_space
    except (TypeError, ValueError):
        space_no = configured_space
    try:
        config = map_config(space_no=space_no)
    except StopIteration:
        space_no = configured_space
        config = map_config()
    session.space_no = space_no
    client_settings = {}
    for key, raw in (character.get('client_settings') or {}).items():
        try:
            value = bytes.fromhex(raw) if isinstance(raw, str) else bytes(raw)
            # ClientSetting.initValue performs cPickle.loads(value) on the
            # client.  Keep the original pickle bytes opaque on the server;
            # unpickling client supplied data here would execute arbitrary
            # reducers and is unnecessary for persistence.
            client_settings[int(key)] = value
        except (TypeError, ValueError, UnicodeError):
            LOG.warning('Ignoring malformed saved client setting: %s', key)
    overrides = {
        'playerName': session.username,
        'roleName': bytes.fromhex(character['name']), 'roleDBID': character['dbid'],
        'clevel': character['level'], 'spaceNo': space_no, 'visible': 1,
        'physique': dict(zip(('school', 'sex', 'facestyle', 'hairstyle', 'haircolor'), physique)),
        'aspect': visible_aspect(character),
        'combatAttr': combat_attributes(character),
        'clientSettings': client_settings, 'channelBlacklist': set(), 'wufu': {},
        'exp': character['experience'], 'bindedCash': character['currency']['binded_cash'],
        'potential': potential(character),
    }
    from quest_service import client_quest_data, client_quest_flags
    overrides['questData'] = client_quest_data(character)
    overrides['questFlag'] = client_quest_flags(character)
    overrides['cash'] = character['currency']['cash']
    session.entity_id = 1002
    from team_service import property_value as team_property
    overrides['team'] = team_property(session, character)
    overrides['battleArrays'] = [1]
    base, base_fields = avatar_properties('base', overrides)
    cell, cell_fields = avatar_properties('cell', overrides)
    session.entity_id = 1002
    avatar = variable_message(5, struct.pack('<IH', session.entity_id, 1) + base)
    # resetEntities(false), then a new Avatar proxy replaces the Account.
    begin = b'\x04\x00' + avatar
    # Loading a different geometry into a reused native space can retain old
    # chunk/entity bindings. Allocate a fresh space even when recovering the
    # same logical map; this ID must survive server/channel restoration.
    previous_space = getattr(session, 'engine_space_id', 0)
    if not previous_space and getattr(session, 'world_started', False):
        previous_space = 1  # Channels created by the legacy implementation.
    space_id = previous_space + 1
    session.engine_space_id = space_id
    from npc_service import catalog as npc_catalog
    path = next(p for p, m in npc_catalog()['maps'].items() if m is config).encode('ascii')
    mapping = mapping_message(path.decode('ascii'), space_id=space_id)
    fallback = tuple(float(v) for v in os.environ.get('WORLD_POSITION', '0,10,0').split(','))
    saved_position = character.get('position')
    if isinstance(saved_position, (list, tuple)) and len(saved_position) == 3:
        try:
            position = tuple(float(v) for v in saved_position)
        except (TypeError, ValueError):
            position = fallback
    else:
        position = fallback
    cell_player = variable_message(6, struct.pack('<II6f', space_id, 0,
                                                 *position, 0.0, 0.0, 0.0) + cell)
    # Seven inherited iCellWithClient methods precede Avatar's methods.
    enable_methods = avatar_client_message(session, 'enable_client_methods')
    LOG.info('World initialization: base_properties=%d bytes=%d cell_properties=%d bytes=%d map=%s engine_space=%d',
             len(base_fields), len(base), len(cell_fields), len(cell), path.decode(), space_id)
    session.world_started = True
    session.world_ready = False
    session.tour_running = False
    session.tour_due = 0
    session.npc_dialog = None
    session.visible_npcs = []
    session.visible_companions = {}
    session.dungeon_switch_states = {}
    session.quest_guide = None
    session.viewport_id = 0
    session.scene_transfer = {}
    session.position = list(position)
    # Persist the map used by this world as soon as an old character enters it.
    # This also upgrades records created before position persistence existed.
    from character_store import update
    update(session, position=list(position), space_no=space_no)
    # The CSXY player script sets its loading target in onGeometryMapped.
    # Deliver the mapping after the player has a space so it receives that
    # callback, rather than relying on an engine-specific replay callback.
    from inventory_service import messages as inventory_messages
    from mount_service import messages as mount_messages
    # server_time initializes Quest and immediately renders its tracked item
    # counts. Populate Package first so login starts at the saved count.
    messages = ([begin, cell_player, mapping, enable_methods] + inventory_messages(session, character)
                + summon_messages(session, character) + mount_messages(session, character)
                + [time_message(session)])
    # Shortcut.restore calls SkillUpdator.check and saves away slots whose
    # skills are not yet learned. Skills are sent in scene_loaded, so defer
    # shortcut restoration until AFTER those messages have reached the client.
    session.shortcut_restore_pending = character.get('shortcut') or ''
    return messages


def reload_scene(session):
    """Wait for the native reset acknowledgement before creating a new player."""
    old_space = getattr(session, 'engine_space_id', 0) or 1
    removals = [mapping_message(remove=True, space_id=old_space)]
    pending = getattr(session, 'scene_transfer', {})
    if pending and pending['old_space'] != old_space:
        removals.append(mapping_message(remove=True, space_id=pending['old_space']))
    messages = initial_messages(session)
    # resetEntities causes the client to send enableEntities (wire ID 8).
    # Its old player, camera and loading callbacks must finish destruction
    # before the new Avatar is created. Sending both in one burst can leave
    # LoadingProgress bound to the destroyed player's empty camera space.
    session.scene_messages = [messages[0][2:].hex()] + [m.hex() for m in messages[1:]]
    LOG.info('Scene reset pending: map=%d engine_space=%d', session.space_no, session.engine_space_id)
    return removals + [b'\x04\x00']


def finish_scene_reset(session):
    messages = getattr(session, 'scene_messages', [])
    if not messages:
        return []
    session.scene_messages = []
    LOG.info('Scene reset acknowledged: map=%d engine_space=%d', session.space_no, session.engine_space_id)
    return [bytes.fromhex(message) for message in messages]


def viewport_message(entity_id, space_id, viewport_id):
    # CSXY v43 Client ID 8: fixed 13 bytes, not a variable message.
    # Native 0x503f00 reads reference EID, cell EID, space ID, viewport byte.
    return b'\x08' + struct.pack('<IIIB', 0, entity_id, space_id, viewport_id)


def transfer_scene(session):
    """Teleport the existing player into a fresh native space and viewport."""
    if not getattr(session, 'engine_space_id', 0):
        return reload_scene(session)
    character = next(c for c in characters(session) if c['dbid'] == session.selected_character)
    plan = prepare_scene_transfer(session, character)
    return apply_scene_transfer(session, plan) if plan else []


def prepare_scene_transfer(session, character):
    """Encode a landing without changing runtime or durable player state."""
    if (not session.world_ready or not getattr(session, 'engine_space_id', 0)
            or getattr(session, 'scene_messages', []) or getattr(session, 'scene_transfer', {})
            or getattr(session, 'superseded', False)):
        return None
    config = map_config(space_no=character['space_no'])
    from npc_service import catalog as npc_catalog
    path = next(p for p, m in npc_catalog()['maps'].items() if m is config)
    old_space = session.engine_space_id
    old_viewport = getattr(session, 'viewport_id', 0)
    viewport = (old_viewport + 1) % 255
    space_id = old_space + 1
    position = list(character['position'])
    packets = [variable_message(14, struct.pack('<I', entity)) for entity in session.visible_npcs]
    packets.extend(variable_message(14, struct.pack('<I', entity))
                   for entity in getattr(session, 'visible_companions', {}))
    # Keep the player, its camera and its loading callback alive. A proxy reset
    # is reserved for login/recovery. The native filter invokes onTeleport for
    # this forced space/position change (0x5038d0 -> Entity/filter callbacks).
    token = uuid.uuid4().bytes
    # _onLoaded calls AutoPatrol.endTeleport on EVERY load, but only calls
    # onClientLoadScene when firstLoad is true. Arm its original UUID callback
    # before moving the player, so later travel reports onTeleportOver(token).
    packets += [avatar_client_message(session, 'begin_teleport', token),
                avatar_property_message(session, 'spaceNo', character['space_no']),
                viewport_message(session.entity_id, space_id, viewport),
                bytes([17, viewport]),
                b'\x34' + struct.pack('<III6f', session.entity_id, space_id, 0,
                                       *position, 0.0, 0.0, 0.0),
                mapping_message(path=path, space_id=space_id)]
    return {'packets': packets, 'space_no': character['space_no'], 'position': position,
            'scene_transfer': {'old_space': old_space, 'old_viewport': old_viewport,
                               'space': space_id, 'viewport': viewport, 'token': token.hex()}}


def apply_scene_transfer(session, plan):
    """Publish a fully encoded scene plan after its save transaction commits."""
    pending = plan['scene_transfer']
    space_id, viewport, old_space = pending['space'], pending['viewport'], pending['old_space']
    session.space_no = plan['space_no']
    session.engine_space_id = space_id
    session.viewport_id = viewport
    session.scene_transfer = pending
    session.position = plan['position']
    session.world_ready = False
    session.visible_npcs = []
    session.visible_companions = {}
    session.dungeon_switch_states = {}
    session.npc_dialog = None
    session.tour_running = False
    session.tour_due = 0
    LOG.info('Scene teleport pending: map=%d engine_space=%d viewport=%d from_space=%d',
             session.space_no, space_id, viewport, old_space)
    return plan['packets']


def finish_scene_transfer(session):
    pending = getattr(session, 'scene_transfer', {})
    if not pending:
        return []
    session.scene_transfer = {}
    # Release the source only after the target loader has finished. The native
    # viewport removal reports spaceGone when its last reference is removed.
    LOG.info('Scene teleport loaded: map=%d engine_space=%d viewport=%d',
             session.space_no, session.engine_space_id, session.viewport_id)
    return [viewport_message(0, pending['old_space'], pending['old_viewport']),
            mapping_message(remove=True, space_id=pending['old_space'])]


def scene_loaded(session, teleport_token=None):
    """Accept the original initial-load or matching later teleport callback."""
    pending = getattr(session, 'scene_transfer', {})
    if getattr(session, 'superseded', False) or getattr(session, 'scene_messages', []):
        return []
    if teleport_token is not None:
        if not pending or pending.get('token') != teleport_token.hex():
            LOG.info('Ignoring stale or unrelated teleport completion')
            return []
    elif pending.get('token'):
        LOG.info('Ignoring initial-load callback while awaiting teleport UUID')
        return []
    session.world_ready = True
    responses = finish_scene_transfer(session)
    from npc_service import update_interest
    responses += update_interest(session, include_companions=False)
    from tutorial_service import resume_tour
    responses += resume_tour(session)
    from quest_story import resume
    responses += resume(session)
    from summon_service import messages as summon_messages
    from mount_service import messages as mount_messages
    from battle_service import selected
    from skill_service import messages as skill_messages
    character = selected(session)
    responses += summon_messages(session, character) + mount_messages(session, character) + skill_messages(session, character)
    from team_service import messages as team_messages
    responses += team_messages(session, character)
    from dungeon_service import messages as dungeon_messages
    responses += dungeon_messages(session, character, loaded=True)
    from companion_scene import update_interest as companion_interest
    responses += companion_interest(session, character)
    shortcut = getattr(session, 'shortcut_restore_pending', '')
    if shortcut:
        try:
            responses.append(avatar_client_message(session, 'update_shortcut',
                                                    shortcut_wire_payload(shortcut)))
            LOG.info('Shortcut restored after skills: character=%d bytes=%d',
                     session.selected_character, len(shortcut) // 2)
        except (TypeError, ValueError):
            LOG.warning('Ignoring malformed saved shortcut')
    session.shortcut_restore_pending = ''
    return responses
