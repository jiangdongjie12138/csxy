"""Recoverable butterfly tour and individually confirmed sister conversations."""
import functools
import json
import logging
import time

import character_store
from attribute_service import notice
from csxy_protocol import ROOT
from entity_methods import avatar_client_message, avatar_property_message

LOG = logging.getLogger('tutorial')
TOUR = 45199
SISTERS = 45200


@functools.lru_cache(maxsize=1)
def catalog():
    return json.loads((ROOT / 'server/data/tutorial_route.json').read_text('utf-8'))


def selected(session):
    return next((c for c in character_store.characters(session)
                 if c['dbid'] == session.selected_character), None)


def nodes():
    return [value for key, value in sorted(catalog()['patrol']['node'].items(),
                                         key=lambda pair: int(pair[0]))]


def near(position, target):
    return sum((position[i] - target[i]) ** 2 for i in range(3)) <= (
        catalog()['simulator_rules']['arrival_radius'] ** 2)


def tour_visuals(session, active):
    rules = catalog()['simulator_rules']
    return [avatar_property_message(session, 'states', [rules['transform_state']] if active else []),
            avatar_property_message(session, 'followNpcID', rules['follower_group'] if active else 0)]


def clear_tour_messages(session):
    session.tour_running = False
    session.tour_due = 0
    return [avatar_client_message(session, 'end_patrol')] + tour_visuals(session, False)


def resume_tour(session):
    if not session.world_ready or getattr(session, 'battle_id', '') or getattr(session, 'tour_running', False):
        return []
    character = selected(session)
    progress = (character or {}).get('tasks', {}).get(str(TOUR), {})
    if progress.get('status') != 'active' or progress.get('tour_phase') == 'arrived':
        return []
    remaining = nodes()[progress.get('tour_next', 0):]
    if not remaining:
        # A disconnected client may have reached the final point before its
        # callback arrived. Require a fresh, bounded final patrol confirmation.
        remaining = nodes()[-1:]
    section = {'patrol_type': remaining[0]['patrol_type'],
               'pos_list': [{'pos': node['pos'], 'speed': node['speed']} for node in remaining]}
    # Encode before changing durable state; an unsupported shape cannot trap a
    # character in a tour it never received.
    responses = tour_visuals(session, True) + [
        avatar_client_message(session, 'begin_patrol', catalog()['simulator_rules']['patrol_id']),
        avatar_client_message(session, 'patrol', section)]

    def change(character):
        character['tasks'][str(TOUR)].update(tour_phase='moving', tour_reported=False)

    character_store.transact(session, change)
    session.tour_running = True
    session.tour_due = time.time() + 90
    LOG.info('Butterfly tour started/resumed: remaining_nodes=%d', len(remaining))
    return responses


def movement(session, report_finished=False):
    character = selected(session)
    progress = (character or {}).get('tasks', {}).get(str(TOUR), {})
    if progress.get('status') != 'active' or progress.get('tour_phase') != 'moving':
        return []
    route = nodes()
    changed = []

    def change(character):
        task = character['tasks'][str(TOUR)]
        index = task.get('tour_next', 0)
        while index < len(route) and near(session.position, route[index]['pos']):
            index += 1
        task['tour_next'] = index
        if report_finished:
            task['tour_reported'] = True
        if (task.get('tour_reported') and index == len(route)
                and near(session.position, route[-1]['pos'])):
            task.update(status='ready', tour_phase='arrived')
            changed.append(True)

    character_store.transact(session, change)
    if changed:
        LOG.info('Butterfly tour arrival saved after route and client confirmation')
        # The original quest has no cq counter. Keep its schema intact and
        # release forced movement and transformation on actual arrival.
        return clear_tour_messages(session) + [notice(session, '游览结束，请与浅笑的玉珠交谈。')]
    return []


def patrol_result(session, success):
    if not getattr(session, 'tour_running', False):
        return []
    if success:
        return movement(session, report_finished=True)

    def change(character):
        task = character['tasks'].get(str(TOUR), {})
        if task.get('status') == 'active':
            task.update(tour_phase='paused', tour_reported=False)

    character_store.transact(session, change)
    return clear_tour_messages(session) + [notice(session, '游览已中断，与梦珠或玉珠交谈可继续，也可重新登录。')]


def tick(session, now=None):
    if (getattr(session, 'tour_running', False)
            and (time.time() if now is None else now) >= session.tour_due):
        return patrol_result(session, False)
    return []


def confirm_sister(session, npc, generation):
    definition = catalog()['sisters'].get(str(npc['template_id']))
    if definition is None or not near(session.position, npc['position']):
        return []
    changed = []
    counter, amount = definition['gain_counters']

    def change(character):
        task = character['tasks'].get(str(SISTERS), {})
        if character.get('space_no') != 14 or task.get('status') not in ('active', 'ready'):
            return
        if not generation or task.get('dialogue_generation') != generation:
            return
        counters = task['data'].setdefault('5', {})
        if counters.get(str(counter), 0) >= amount:
            return
        counters[str(counter)] = amount
        if all(counters.get(str(number), 0) >= 1 for number in range(6561, 6567)):
            task['status'] = 'ready'
        changed.append(True)

    character = character_store.transact(session, change)
    if not changed:
        return []
    counters = {int(key): value for key, value in character['tasks'][str(SISTERS)]['data']['5'].items()}
    LOG.info('Sister conversation confirmed and saved: counter=%d', counter)
    return [avatar_client_message(session, 'update_quest_data', SISTERS, 5, counters)]
