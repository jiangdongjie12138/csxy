"""Original transport choices, durable positions, and map loading."""
import uuid

import character_store
from attribute_service import notice
from quest_catalog import catalog


# Original phase B exit from full_seek_space_data.json.
PHASE_EXIT_POSITIONS = {
    (16, 14): (236.83, 30.64, -80.49),
}


def permitted(session, npc, character):
    return (session.world_ready and not getattr(session, 'battle_id', '')
            and npc['entity_id'] in session.visible_npcs
            and npc['space_no'] == character.get('space_no')
            and sum((session.position[i] - npc['position'][i]) ** 2 for i in (0, 2)) <= 25 ** 2)


def open_transport(session, npc):
    from battle_service import selected
    from npc_service import npc_client_message
    character = selected(session)
    if character is None or not permitted(session, npc, character):
        return []
    data = catalog()['transport'][str(npc['template_id'])]
    choices = [(n, d[0].encode('gbk')) for n, d in enumerate(data.get('destinations', []))]
    choices.append((255, '告辞'.encode('gbk')))
    token = uuid.uuid4().bytes
    session.npc_dialog = {'entity_id': npc['entity_id'], 'token': token.hex(), 'action': 'transport'}
    return [npc_client_message(npc, 'dialog_on_cell', token,
                               bytes.fromhex(npc['gossip_gbk'][0]) if npc['gossip_gbk'] else b'', choices, 0)]


def transport(session, npc, choice):
    from battle_service import selected
    from npc_service import map_config
    character = selected(session)
    if not character or not permitted(session, npc, character):
        return []
    data = catalog()['transport'][str(npc['template_id'])]
    destinations = data.get('destinations', [])
    if choice == 255:
        return []
    if not 0 <= choice < len(destinations):
        return []
    if character['level'] < data.get('req_level', 1):
        return [notice(session, '需要达到%d级才能前往。' % data['req_level'])]
    from quest_catalog import active
    if data.get('quests') and not any(active(character, n) for n in data['quests']):
        return [notice(session, '请先接取对应任务。')]
    cost = data.get('cost', 0)
    if character['currency']['cash'] < cost:
        return [notice(session, '金钱不足，需要%d。' % cost)]
    _, target_space, position, *_ = destinations[choice]
    try:
        map_config(space_no=target_space)
    except StopIteration:
        return [notice(session, '暂时无法到达该地点。')]
    if position == [999.0, 999.0, 999.0]:
        # Phase B has an explicit exit; other sentinel portals retain position.
        source_space = int(character.get('space_no') or getattr(session, 'space_no', 0) or 0)
        position = list(PHASE_EXIT_POSITIONS.get((source_space, target_space), session.position))
    def change(c):
        if c['currency']['cash'] < cost:
            raise ValueError('insufficient transport fare')
        c['currency']['cash'] -= cost
        c.update(space_no=target_space, position=list(position))
    character_store.transact(session, change)
    from world_service import transfer_scene
    return transfer_scene(session)
