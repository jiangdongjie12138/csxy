"""Original Dak map UI, real station landings, and atomic travel fares."""
import functools
import json
import logging
import math

import character_store
from attribute_service import notice
from csxy_protocol import ROOT
from entity_methods import avatar_client_message
from npc_service import entity_index, npc_client_message
from world_service import apply_scene_transfer, prepare_scene_transfer

LOG = logging.getLogger('dak')


@functools.lru_cache(maxsize=1)
def rules():
    return json.loads((ROOT / 'server/data/dak_rules.json').read_text('utf-8'))


@functools.lru_cache(maxsize=1)
def stations():
    return {n['template_id']: n for n in entity_index().values() if n['type'] == 'Dak'}


def permitted(session, npc, character):
    battle = character.get('world_state', {}).get('tutorial_battle', {})
    return (not getattr(session, 'superseded', False) and session.world_ready
            and getattr(session, 'engine_space_id', 0) > 0
            and not getattr(session, 'scene_messages', [])
            and not getattr(session, 'scene_transfer', {})
            and not getattr(session, 'battle_id', '')
            and battle.get('phase') not in ('preparing', 'input', 'animating')
            and npc['type'] == 'Dak' and npc == stations().get(npc['template_id'])
            and npc['entity_id'] in session.visible_npcs
            and npc['space_no'] == character.get('space_no') == session.space_no
            and len(session.position) == 3
            and all(math.isfinite(v) for v in session.position)
            and sum((session.position[i] - npc['position'][i]) ** 2 for i in (0, 2)) <= 25 ** 2)


def open_station(session, npc):
    character = next((c for c in character_store.characters(session)
                      if c['dbid'] == session.selected_character), None)
    if character is None or not permitted(session, npc, character):
        return []
    return [npc_client_message(npc, 'showMap')]


class TravelRejected(ValueError):
    pass


def goto_station(session, npc, target_id):
    # The client calls the SOURCE entity with a destination NPC_ID (UINT32).
    # Template IDs are unrelated to logical map numbers or TransportMan IDs.
    plan = None
    cash_message = None
    fare = 0

    def change(character):
        nonlocal plan, cash_message, fare
        if not permitted(session, npc, character):
            return
        data = rules()
        source = data['templates'][str(npc['template_id'])]
        target = stations().get(target_id)
        if target_id == npc['template_id']:
            raise TravelRejected('你已在该区域。')
        if (target is None or not any(route[0] == target_id for route in source['destinations'])):
            raise TravelRejected('该驿站暂时无法到达。')
        minimum = max(source.get('req_level', 1),
                      data['templates'][str(target_id)].get('req_level', 1))
        if character['level'] < minimum:
            raise TravelRejected('需要达到%d级才能使用该驿站。' % minimum)
        fare = data['fare'] if character['level'] >= data['free_below_level'] else 0
        if character['currency']['cash'] < fare:
            raise TravelRejected('金钱不足，驿站传送需要%d金钱。' % fare)
        # Only the transaction's working copy changes here. Encode every reply
        # before publishing the new save, so encoding/I/O errors charge nothing.
        character['currency']['cash'] -= fare
        character.update(space_no=target['space_no'], position=list(target['position']))
        cash_message = avatar_client_message(session, 'update_cash', character['currency']['cash'])
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
    LOG.info('Dak travel committed: source=%d target=%d map=%d fare=%d cash=%d',
             npc['template_id'], target_id, character['space_no'], fare, character['currency']['cash'])
    return [cash_message] + packets
