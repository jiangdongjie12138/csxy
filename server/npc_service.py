"""Static map NPCs and per-player area of interest."""
import functools
import json
import logging
import os
import struct
import xml.etree.ElementTree as ET

from csxy_protocol import ROOT, variable_message
from entity_schema import npc_properties
from entity_methods import client_message, decode_slot, methods

LOG = logging.getLogger('npc')
ENTER_RADIUS = 150.0
LEAVE_RADIUS = 180.0


@functools.lru_cache(maxsize=1)
def catalog():
    return json.loads((ROOT / 'server/data/world_npcs.json').read_text('utf-8'))


def map_config(session=None, space_no=None):
    if space_no is None and session is not None:
        space_no = getattr(session, 'space_no', None)
        if space_no is None:
            from character_store import characters
            character = next((c for c in characters(session) if c['dbid'] == session.selected_character), None)
            space_no = (character or {}).get('space_no')
    if space_no is not None:
        return next(m for m in catalog()['maps'].values() if m['space_no'] == space_no)
    name = os.environ.get('WORLD_MAP', 'universes/eg/xinsd')
    try:
        return catalog()['maps'][name]
    except KeyError:
        raise ValueError('map has no recovered configuration: ' + name)


@functools.lru_cache(maxsize=1)
def entity_index():
    return {npc['entity_id']: npc for config in catalog()['maps'].values() for npc in config['npcs']}


def find_npc(entity_id, session=None):
    if session is not None:
        guide = getattr(session, 'quest_guide', None)
        if guide and guide['entity_id'] == entity_id:
            return guide
        from dungeon_service import find_npc as dungeon_npc
        dynamic = dungeon_npc(session, entity_id)
        if dynamic:
            return dynamic
    return entity_index().get(entity_id)


def npc_by_template(template_id, space_no=None, kind=None):
    return next((n for n in entity_index().values() if n['template_id'] == template_id
                 and (space_no is None or n['space_no'] == space_no)
                 and (kind is None or n['type'] == kind)), None)


@functools.lru_cache(maxsize=None)
def _definitions(definition):
    """Resolve the NPC interfaces that contribute network methods."""
    root = ET.parse((ROOT / 'analysis' / 'resources' / definition)).getroot()
    if definition == 'record_0257.xml':
        return ('record_0224.xml', 'record_0118.xml', definition)
    parent = root.findtext('Implements/Interface')
    if parent not in ('iNpc', 'iClanNpc'):
        raise ValueError('unsupported NPC interface: ' + str(parent))
    roots = ('record_0224.xml', 'record_0118.xml', 'record_0316.xml')
    # iClanNpc adds private clan fields, but no Cell/Client methods.
    if parent == 'iClanNpc':
        roots += ('record_0280.xml',)
    return roots + (definition,)


def npc_methods(npc, domain):
    if npc['type'] == 'DungeonSwitch':
        return methods(('record_0224.xml', 'record_0118.xml', 'record_0124.xml'), domain)
    definition = catalog()['types'][npc['type']]['definition']
    return methods(_definitions(definition), domain)


def cell_method_name(npc, slot):
    """Resolve a captured NPC method slot without guessing its arguments."""
    table = npc_methods(npc, 'CellMethods')
    return table[slot].tag if 0 <= slot < len(table) else 'unknown'


def npc_client_message(npc, name, *arguments):
    definition = catalog()['types'][npc['type']]['definition']
    return client_message(npc['entity_id'], _definitions(definition), name, *arguments)


def interact(session, npc, slot, arguments):
    """Reply to captured NPC clicks using original client-side dialogs."""
    if (not session.world_ready or npc['entity_id'] not in session.visible_npcs
            or len(session.position) != 3):
        LOG.info('NPC interaction ignored: entity not in ready player interest')
        return []
    from character_store import characters
    character = next((c for c in characters(session) if c['dbid'] == session.selected_character), None)
    if character and character.get('space_no') is not None and npc.get('space_no') != character['space_no']:
        return []
    method, arguments = decode_slot(npc_methods(npc, 'CellMethods'), slot, arguments)
    LOG.info('NPC entity=%d type=%s method=%s slot=%d payload_bytes=%d',
             npc['entity_id'], npc['type'], method.tag, slot, len(arguments))
    from dungeon_service import request as dungeon_request
    if method.tag == 'use' and arguments:
        raise ValueError('NPC use takes no arguments')
    dungeon_replies = dungeon_request(session, npc, method.tag, arguments)
    if dungeon_replies is not None:
        return dungeon_replies
    if method.tag == 'use':
        if arguments:
            raise ValueError('NPC use takes no arguments')
        if npc.get('quest_guide'):
            from quest_offline import open_menu
            return open_menu(session, npc)
        if npc['type'] == 'QuestMan':
            from quest_story import resume
            story = resume(session, npc)
            if story:
                return story
            # Yu Zhu remains visible as the completion NPC while the original
            # Dream Pearl may hide after acceptance. Either can resume a tour.
            if npc['template_id'] in (21138, 21162):
                from tutorial_service import resume_tour
                resumed = resume_tour(session)
                if resumed:
                    return resumed
            from quest_event_service import talk_quest_npc
            return talk_quest_npc(session, npc) + [npc_client_message(npc, 'showQuests')]
        if npc['type'] == 'Monster':
            from battle_service import start
            return start(session, npc)
        if npc['type'] == 'SpellMaster':
            from skill_service import catalog as skill_catalog
            from attribute_service import notice
            master = skill_catalog()['masters'].get(str(npc['template_id']), {})
            if (character is None or master.get('school') != bytes.fromhex(character['physique'])[0]
                    or sum((session.position[i] - npc['position'][i]) ** 2 for i in (0, 2)) > 25 ** 2):
                return [notice(session, '请靠近本流派技能大师。')]
            return [npc_client_message(npc, 'showLearnRoleSpellDlg')]
        if npc['type'] == 'TransportMan':
            from travel_service import open_transport
            return open_transport(session, npc)
        if npc['type'] == 'Dak':
            from dak_service import open_station
            return open_station(session, npc)
        if npc['type'] == 'StoreMan':
            from store_service import query
            return query(session, npc, 0)
        if npc['type'] == 'Idler' and not npc.get('gossip_gbk'):
            from quest_event_service import activate_idler
            return activate_idler(session, npc)
        gossip = npc.get('gossip_gbk', [])
        if gossip:
            # onDialog converts each (option_id, text) into the local dialog
            # script. UUID is an opaque dialog token; no gameplay mutation.
            # A fresh token binds each confirmed conversation to its opening;
            # replayed answers cannot complete a later acceptance of the quest.
            import uuid
            token = uuid.uuid4().bytes
            from tutorial_service import selected, SISTERS
            character = selected(session)
            generation = (character or {}).get('tasks', {}).get(str(SISTERS), {}).get('dialogue_generation')
            session.npc_dialog = {'entity_id': npc['entity_id'], 'token': token.hex(),
                                  'generation': generation}
            return [npc_client_message(npc, 'dialog_on_cell', token,
                                       bytes.fromhex(gossip[0]), [(0, b'\xb8\xe6\xb4\xc7')], 0)]
    elif method.tag == 'learnRoleSpell' and npc['type'] == 'SpellMaster':
        if len(arguments) != 2:
            raise ValueError('learnRoleSpell needs SPELL_ID')
        from skill_service import learn
        return learn(session, struct.unpack('<H', arguments)[0], npc)
    elif method.tag == 'acceptQuest':
        if len(arguments) != 4:
            raise ValueError('NPC quest acceptance needs one QUEST_NO')
        from quest_service import accept
        return accept(session, npc, struct.unpack('<I', arguments)[0])
    elif method.tag in ('completeQuest', 'completeQuestWithItem'):
        if len(arguments) != (4 if method.tag == 'completeQuest' else 7):
            raise ValueError('NPC quest completion needs one QUEST_NO')
        from quest_service import complete
        selected = None
        if method.tag == 'completeQuestWithItem':
            if arguments[4] != 1:
                return []
            selected = struct.unpack_from('<bb', arguments, 5)
        return complete(session, npc, struct.unpack_from('<I', arguments)[0], selected)
    elif method.tag in ('preBeginCircleQuest', 'beginCircleQuest'):
        if len(arguments) != 4:
            raise ValueError('Circle quest needs one library ID')
        identifier = struct.unpack('<I', arguments)[0]
        from quest_controller import auxiliary
        lib = auxiliary()['circle_quest_lib'].get(str(identifier), {})
        if npc['template_id'] not in lib.get('aquire_npcs', []) or lib.get('shuted'):
            return []
    elif npc['type'] == 'StoreMan' and method.tag in ('query', 'buy', 'sell', 'retrieve', 'retrieveAll'):
        from store_service import interact as store_interact
        return store_interact(session, npc, method.tag, arguments)
        if method.tag == 'preBeginCircleQuest':
            return [npc_client_message(npc, 'showCircleQuest', identifier)]
        from quest_offline import circle_begin
        return circle_begin(session, npc, identifier)
    elif method.tag == 'beginBattle':
        if arguments:
            raise ValueError('beginBattle takes no arguments')
        from quest_catalog import active, definition
        from battle_service import start
        for key, progress in character['tasks'].items():
            if active(character, int(key)) and definition(int(key)).get('custom_battle') and npc['template_id'] in definition(int(key)).get('aquire_npcs', []):
                return start(session, npc, story_quest=int(key))
        return []
    elif method.tag == 'inputAnswer':
        from csxy_protocol import packed_bytes
        token, offset = packed_bytes(arguments, 0)
        value, end = packed_bytes(arguments, offset)
        dialog = getattr(session, 'npc_dialog', None)
        if (end != len(arguments) or not dialog or dialog.get('action') != 'quest_search'
                or dialog['token'] != token.hex() or dialog['entity_id'] != npc['entity_id']
                or dialog.get('expires', 0) < __import__('time').time()):
            return []
        from quest_event_service import near
        if not near(session, npc, character):
            return []
        session.npc_dialog = None
        from quest_offline import quest_menu
        try:
            number = int(value.decode('ascii'))
        except ValueError:
            return []
        return quest_menu(session, npc, number)
    elif method.tag in ('showQuest', 'showCompleteQuest'):
        if len(arguments) != 4:
            raise ValueError('NPC quest preview needs one QUEST_NO')
        quest_no = struct.unpack('<I', arguments)[0]
        from quest_catalog import preview_allowed
        if preview_allowed(session, npc, quest_no, method.tag):
            return [npc_client_message(npc, method.tag, quest_no)]
        LOG.info('NPC quest preview rejected: template=%d quest=%d',
                 npc['template_id'], quest_no)
        from quest_catalog import supported, definition as quest_definition
        q = quest_definition(quest_no)
        if (q and not q.get('shuted') and quest_no not in supported()
                and npc['template_id'] in q.get('aquire_npcs', []) + q.get('complete_npcs', [])):
            from attribute_service import notice
            return [notice(session, '该任务暂未开放，请选择其他任务。')]
    elif method.tag == 'stopTalk':
        if arguments:
            raise ValueError('NPC stopTalk takes no arguments')
    elif method.tag == 'dialogAnswer':
        from csxy_protocol import packed_bytes
        token, offset = packed_bytes(arguments, 0)
        dialog = getattr(session, 'npc_dialog', None)
        if (not dialog or dialog['entity_id'] != npc['entity_id'] or dialog['token'] != token.hex()
                or len(arguments[offset:]) != 1):
            return []  # Delayed/repeated answer to a dialog no longer open.
        selection = arguments[offset]
        if dialog.get('action') == 'dungeon':
            session.npc_dialog = None
            from dungeon_service import answer
            return answer(session, npc, dialog, selection)
        if dialog.get('action') == 'quest_offline':
            session.npc_dialog = None
            from quest_offline import answer
            try:
                return answer(session, npc, dialog, selection)
            except ValueError as error:
                from attribute_service import notice
                return [notice(session, str(error))]
        if dialog.get('action') == 'transport':
            session.npc_dialog = None
            from travel_service import transport
            return transport(session, npc, selection)
        if selection != 0:
            return []
        session.npc_dialog = None
        if npc['type'] == 'Idler':
            if npc['template_id'] in (14087, 14078, 14079, 14080, 14081, 14082):
                from tutorial_service import confirm_sister
                return confirm_sister(session, npc, dialog['generation'])
            from quest_event_service import activate_idler
            return activate_idler(session, npc)
    elif method.tag == 'gotoDak' and npc['type'] == 'Dak':
        if len(arguments) != 4:
            raise ValueError('gotoDak needs one UINT32 NPC_ID')
        from dak_service import goto_station
        return goto_station(session, npc, struct.unpack('<I', arguments)[0])
    elif method.tag == 'transport' and npc['type'] == 'TransportMan':
        if len(arguments) != 1:
            raise ValueError('transport needs UINT8 destination index')
        from travel_service import transport
        return transport(session, npc, arguments[0])
    else:
        LOG.info('NPC gameplay method awaiting implementation: %s', method.tag)
    return []


def create_npc(npc):
    if npc['type'] == 'DungeonSwitch':
        from dungeon_service import create_switch
        return create_switch(npc)
    definition = catalog()['types'][npc['type']]
    properties = npc_properties(definition['definition'], {
        'visible': 1, 'roleNameEx': bytes.fromhex(npc['name_gbk']),
        'titleEx': bytes.fromhex(npc['title_gbk']), 'aspect': npc['aspect'],
        'fightFlag': 0, definition['id_property']: npc['template_id'],
        'level': npc.get('level', 0),
    })
    # CSXY's native createEntity handler is 0x504310, wire ID 9.
    # This build reads EID, type, position, three compressed direction bytes
    # directly (the later engine's compression wrapper is absent).
    payload = struct.pack('<IH3f3b', npc['entity_id'], definition['client_type'],
                          *npc['position'], 0, 0, 0) + properties
    return variable_message(9, payload)


def enter_npc(npc):
    # enterAoI (0x5040b0) precedes createEntity so the enter count is positive.
    return b'\x0b' + struct.pack('<IB', npc['entity_id'], 255) + create_npc(npc)


def quest_visible(npc, character):
    """Apply recovered explicit quest visibility; unimplemented states stay separate."""
    conditions = npc.get('visible', {})
    tasks = character.get('tasks', {}) if character else {}
    required = conditions.get('quests', [])
    if required and not any(tasks.get(str(number), {}).get('status') in ('active','ready')
                            for number in required):
        return False
    if any(tasks.get(str(number), {}).get('status') == 'completed'
           for number in conditions.get('done_quests_hide', [])):
        return False
    states = (character or {}).get('world_state', {}).get('quest_states', {})
    if any(states.get(str(n), 0) for n in conditions.get('hide_states', [])):
        return False
    if conditions.get('hide_if_no_quest') and npc['type'] == 'QuestMan':
        from quest_catalog import can_accept, active
        if not any(active(character, n) for n in npc.get('complete_quests', [])) and not any(
                can_accept(character, n) for n in npc.get('aquire_quests', [])):
            return False
    return True


def update_interest(session, include_companions=True):
    if not session.world_ready or len(session.position) != 3:
        return []
    visible = set(session.visible_npcs)
    wanted = set()
    responses = []
    from character_store import characters
    character = next((c for c in characters(session) if c['dbid'] == session.selected_character), None)
    # An active encounter has its own battle doll.  Keep the source scene
    # monster out of the AOI until battle_service marks the encounter finished,
    # otherwise the client renders a world NPC over the battle monster.
    active_battle = (character or {}).get('world_state', {}).get('tutorial_battle', {})
    hidden_entity = active_battle.get('npc') if active_battle.get('phase') in (
        'preparing', 'input', 'animating') else None
    x, _, z = session.position
    from monster_service import available
    # One native guide provides the restored events missing from the old
    # server scripts. It is anchored to the scene's first loaded position.
    from quest_offline import interest as guide_interest
    map_npcs = map_config(session)['npcs'] + [guide_interest(session)]
    from dungeon_service import scene_npcs, visible as dungeon_visible, switch_messages
    map_npcs += scene_npcs(session, character)
    for npc in map_npcs:
        if npc['entity_id'] == hidden_entity:
            continue
        if not dungeon_visible(npc, character):
            continue
        if npc['type'] == 'Monster' and not available(character, npc):
            continue
        if not quest_visible(npc, character):
            continue
        entity_id = npc['entity_id']
        radius = LEAVE_RADIUS if entity_id in visible else ENTER_RADIUS
        nx, _, nz = npc['position']
        if (x - nx) ** 2 + (z - nz) ** 2 <= radius ** 2:
            wanted.add(entity_id)
            if entity_id not in visible:
                responses.append(enter_npc(npc))
    for entity_id in visible - wanted:
        responses.append(variable_message(14, struct.pack('<I', entity_id)))
    if wanted != visible:
        LOG.info('NPC interest: entered=%d left=%d visible=%d',
                 len(wanted - visible), len(visible - wanted), len(wanted))
    session.visible_npcs = sorted(wanted)
    responses.extend(switch_messages(session, character))
    if include_companions:
        from companion_scene import update_interest as companion_interest
        responses.extend(companion_interest(session, character))
    return responses
