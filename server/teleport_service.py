"""Original world map flag requests and atomic travel within the current map."""
import functools
import json
import logging

import character_store
from attribute_service import notice
from csxy_protocol import ROOT
from entity_methods import avatar_client_message
from npc_service import map_config
from world_service import apply_scene_transfer, prepare_scene_transfer

LOG = logging.getLogger('teleport')


@functools.lru_cache(maxsize=1)
def rules():
    return json.loads((ROOT / 'server/data/teleport_rules.json').read_text('utf-8'))


def permitted(session, character):
    battle = character.get('world_state', {}).get('tutorial_battle', {})
    return (not getattr(session, 'superseded', False) and session.world_ready
            and getattr(session, 'engine_space_id', 0) > 0
            and not getattr(session, 'scene_messages', [])
            and not getattr(session, 'scene_transfer', {})
            and not getattr(session, 'battle_id', '')
            and battle.get('phase') not in ('preparing', 'input', 'animating')
            and session.space_no == character.get('space_no'))


class TravelRejected(ValueError):
    pass


def goto_point(session, target_id):
    # WorldMapBase.onTeleport sends ONE NPC_ID. A point can occur in several
    # maps, so resolve it in the current logical map, never in a global ID dict.
    # The original current-map interface has no activation or distance gate.
    plan = None
    replies = []
    fare = 0

    def change(character):
        nonlocal plan, replies, fare
        if not permitted(session, character):
            return
        data = rules()
        target = next((npc for npc in map_config(space_no=session.space_no)['npcs']
                       if npc['type'] == 'TeleportMan' and npc['template_id'] == target_id), None)
        if target is None or str(target_id) not in data['templates']:
            raise TravelRejected('该传送点不在当前场景。')
        fare = data['fare'] if character['level'] >= data['free_below_level'] else 0
        if character['currency']['cash'] < fare:
            raise TravelRejected('金钱不足，传送需要%d金钱。' % fare)
        character['currency']['cash'] -= fare
        character['position'] = list(target['position'])
        replies = [avatar_client_message(session, 'update_cash', character['currency']['cash']),
                   avatar_client_message(session, 'goto_teleport_point_ok', target_id)]
        plan = prepare_scene_transfer(session, character)
        if plan is None:
            raise TravelRejected('请等待当前场景加载完成。')

    try:
        character = character_store.transact(session, change)
    except TravelRejected as error:
        return [notice(session, str(error))]
    if character is None or plan is None:
        return []
    packets = apply_scene_transfer(session, plan)
    LOG.info('Map flag travel committed: target=%d map=%d position=%s fare=%d cash=%d',
             target_id, character['space_no'], character['position'], fare,
             character['currency']['cash'])
    return replies + packets
