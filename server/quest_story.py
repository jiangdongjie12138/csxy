"""Special tutorial choreography around the general quest transaction engine.

Original scripts, conditions and actor positions are retained. Flight and
encounter numbers use simulator policies where server choreography is absent.
"""
import logging
import time
import uuid
import character_store

from entity_methods import avatar_client_message
from quest_catalog import catalog, active

LOG = logging.getLogger('quest-story')
SCRIPTS = {45201: 2219}
STORY_BATTLES = tuple(catalog()['simulator_rules']['story_battles']) + tuple(
    int(number) for number in catalog()['simulator_rules'].get('custom_battles', {}))
AUTO_START_STORY_BATTLES = tuple(catalog()['simulator_rules']['story_battles']) + (3002,)
TRANSFERS = {45202: (15, 21180), 45204: (14, 14074), 45216: (16, 5156),
             13469: (4, 5148)}
# Original phase B entrance from full_seek_space_data.json.
PHASE_ENTRY_POSITIONS = {
    16: (243.4, 31.24, -72.89),
}


def accepted(character, number):
    from inventory_service import grant_items, QUEST_BAG_PAGE
    if number == 45204 and not any(i['id'] == 58083 for i in character['inventory']):
        grant_items(character, [(58083, 1)], QUEST_BAG_PAGE)
    from quest_catalog import item_count
    supplies = [(i, max(0, n - item_count(character, i)))
                for i, n in catalog()['simulator_rules'].get('acceptance_items', {}).get(str(number), [])]
    if supplies:
        grant_items(character, supplies, QUEST_BAG_PAGE)
    for state in ({45204: (47455, 47456, 47457), 45208: (47462,)}.get(number, ())):
        character['world_state'].setdefault('quest_states', {}).pop(str(state), None)
    if number in SCRIPTS:
        character['tasks'][str(number)]['story_finished'] = False
    # The original quest crosses into these phase maps, rather than granting
    # its next NPC as a remote interaction in the old world.
    if number in TRANSFERS:
        target = TRANSFERS[number]
        from npc_service import npc_by_template
        npc = npc_by_template(target[1], target[0], 'QuestMan' if target[1] != 14074 else 'Idler')
        if npc:
            character['space_no'] = target[0]
            entry = PHASE_ENTRY_POSITIONS.get(target[0])
            character['position'] = (list(entry) if entry else
                                     [npc['position'][0] + 4, npc['position'][1], npc['position'][2] + 4])


def completed(character, number, reward):
    if number == 45219:
        from inventory_service import grant_items, QUEST_BAG_PAGE
        if not any(i['id'] == 74090 for i in character['inventory']):
            reward['item_uuids'].extend(grant_items(character, [(74090, 1)], QUEST_BAG_PAGE))
            reward.setdefault('items', []).append((74090, 1))


def abandoned(character, number):
    pending = character['world_state'].get('quest_script')
    if pending and pending['quest'] == number:
        character['world_state'].pop('quest_script', None)
    action = character['world_state'].get('quest_action', {})
    if str(number) in action.get('generations', {}):
        character['world_state'].pop('quest_action', None)


def objectives_met(character, number, progress):
    if number in SCRIPTS and not progress.get('story_finished'):
        return False
    if number in STORY_BATTLES and not progress.get('battle_won'):
        return False
    return True


def start_script(session, number):
    import character_store
    packets = []

    def change(character):
        if not active(character, number) or character['tasks'][str(number)].get('story_finished'):
            return
        token = uuid.uuid4().bytes
        character['world_state']['quest_script'] = {
            'quest': number, 'token': token.hex(), 'started': time.time(),
            'accepted_at': character['tasks'][str(number)]['accepted_at']}
        packets.append(avatar_client_message(session, 'play_script', SCRIPTS[number], token))

    character_store.transact(session, change)
    return packets


def acceptance_messages(session, number, character):
    packets = []
    if number == 45204 or str(number) in catalog()['simulator_rules'].get('acceptance_items', {}):
        from inventory_service import messages
        packets.extend(messages(session, character))
    if number in TRANSFERS:
        from world_service import transfer_scene
        return packets + transfer_scene(session)
    if number in SCRIPTS:
        packets.extend(start_script(session, number))
    if number in AUTO_START_STORY_BATTLES:
        from battle_service import start_story_battle
        packets.extend(start_story_battle(session, number))
    return packets


def script_finished(session, token, success):
    import character_store
    changed = []

    def change(character):
        pending = character['world_state'].get('quest_script', {})
        number = pending.get('quest')
        progress = character['tasks'].get(str(number), {})
        if (pending.get('token') != token.hex() or not active(character, number)
                or pending.get('accepted_at') != progress.get('accepted_at')
                or time.time() - pending.get('started', 0) > 600):
            return
        character['world_state'].pop('quest_script', None)
        if success:
            progress['story_finished'] = True
            changed.append(number)

    character = character_store.transact(session, change)
    if not changed:
        return []
    LOG.info('Quest script confirmed and saved: quest=%d', changed[0])
    from npc_service import update_interest
    return update_interest(session)


def resume(session, npc=None):
    from battle_service import selected, start_story_battle
    character = selected(session)
    if not character or not session.world_ready or getattr(session, 'battle_id', ''):
        return []
    for number in SCRIPTS:
        if (active(character, number) and not character['tasks'][str(number)].get('story_finished')
                and (npc is None or npc['template_id'] in catalog()['quests'][str(number)].get('aquire_npcs', []))):
            return start_script(session, number)
    for number in AUTO_START_STORY_BATTLES:
        if (active(character, number) and not character['tasks'][str(number)].get('battle_won')
                and npc and npc['template_id'] in (catalog()['quests'][str(number)].get('aquire_npcs', []) +
                                                  catalog()['quests'][str(number)].get('complete_npcs', []))):
            return start_story_battle(session, number)
    return []


def movement(session):
    """Arrival simulation for the missing server escort/chase controller."""
    from battle_service import selected
    from npc_service import npc_by_template
    from quest_service import gain_counters, counter_messages
    c = selected(session)
    changed = []
    for number, rule in catalog()['simulator_rules'].get('arrival_quests', {}).items():
        if not c or not active(c, int(number)) or c.get('space_no') != rule['space']:
            continue
        npc = npc_by_template(rule['npc'], rule['space'], 'QuestMan')
        if (npc and sum((session.position[i] - npc['position'][i]) ** 2 for i in (0, 2)) <= 25 ** 2):
            def change(character):
                updates = gain_counters(character, [(rule['counter'], 1)])
                if updates:
                    character['tasks'][number]['data']['2'] = True
                    changed.extend(updates)
            c = character_store.transact(session, change)
    return counter_messages(session, c, changed) if changed else []
