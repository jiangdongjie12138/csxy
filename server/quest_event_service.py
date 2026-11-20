"""Validated NPC, inventory, timer and linked-quest events."""
import logging
import copy
import functools
import time

import character_store
from attribute_service import notice
from entity_methods import avatar_client_message
from quest_catalog import active, catalog, definition, item_count, supported, can_accept, event_supported, values

LOG = logging.getLogger('quest-events')


def near(session, npc, character):
    return (character is not None and session.world_ready and not getattr(session, 'battle_id', '')
            and npc['entity_id'] in session.visible_npcs
            and npc.get('space_no', character.get('space_no')) == character.get('space_no')
            and sum((session.position[i] - npc['position'][i]) ** 2 for i in (0, 2)) <= 25 ** 2)


def relevant(character, npc):
    gain = npc.get('gain_counters', [])
    items = {row[0] for row in npc.get('gain_items', [])}
    for n in character['tasks']:
        if not active(character, int(n)):
            continue
        req = (definition(int(n)) or {}).get('cq', {}).get('req', {})
        needed = dict(req.get('counters', []))
        progress = character['tasks'][n]['data'].get('5', {})
        if gain and progress.get(str(gain[0]), 0) < needed.get(gain[0], 0):
            return True
        if any(i in items and item_count(character, i) < count for i, count in req.get('items', [])):
            return True
    return False


def eligible(character, npc, item_id=None):
    req = npc.get('req', {})
    if not event_supported(npc, 'idler'):
        return False
    if not req.get('min_lv', 1) <= character['level'] <= req.get('max_lv', 255):
        return False
    if req.get('stat') and not character['world_state'].get('quest_states', {}).get(str(req['stat'])):
        return False
    allowed_items = {row[0] for row in req.get('items', [])} | {req.get('use_item')}
    if item_id is not None and item_id not in allowed_items:
        return False
    if req.get('quests') and not any(active(character, n) for n in req['quests']):
        return False
    if any(item_count(character, row[0]) < row[1] for row in req.get('items', [])):
        return False
    if req.get('use_item') and (item_count(character, req['use_item']) < 1
                               or item_id not in (None, req['use_item'])):
        return False
    return relevant(character, npc)


@functools.lru_cache(maxsize=1)
def linked_definitions():
    return tuple((int(n), q) for n, q in catalog()['quests'].items() if q.get('prior_linked_quest'))


def settle_links(character):
    """Auto-complete only fulfilled nodes with no completion NPC, then link."""
    from quest_service import initialize, objectives_met
    from progression_service import grant_quest_experience
    from inventory_service import grant_reward
    events = []
    for _ in range(20):
        changed = False
        for n, progress in list(character['tasks'].items()):
            number, q = int(n), definition(int(n))
            if (q and active(character, number) and not q.get('complete_npcs')
                    and not q.get('complete_item')
                    and objectives_met(character, number)):
                for identifier, amount in q.get('cq', {}).get('req', {}).get('items', []):
                    consume(character, identifier, amount)
                from quest_controller import consume_extra, completed as controller_completed
                consume_extra(character, number)
                reward = grant_reward(character, number)
                from quest_story import completed
                completed(character, number, reward)
                reward.update(grant_quest_experience(character, q))
                progress.update(status='completed', rewarded=True, reward=reward,
                                completed_at=int(time.time()))
                controller_completed(character, number)
                events.append(('finish', number))
                changed = True
        for number, q in linked_definitions():
            if (str(number) not in character['tasks']
                    and can_accept(character, number, linked=True)):
                initialize(character, number)
                from quest_story import accepted
                accepted(character, number)
                events.append(('accept', number))
                changed = True
        if not changed:
            break
    return events


def linked_messages(session, character, events):
    from quest_service import client_quest_data
    packets = []
    for kind, number in events:
        if kind == 'finish':
            packets.extend([avatar_client_message(session, 'update_quest_flag', number, 1),
                            avatar_client_message(session, 'finish_quest', number, 0)])
            reward = character['tasks'][str(number)].get('reward', {})
            from inventory_service import intro_reward
            parts = ['经验 × %d' % reward['experience']] if reward.get('experience') else []
            parts += ['%s × %d' % (intro_reward()['items'][str(i)]['name'], n)
                      for i, n in reward.get('items', [])]
            if reward.get('cash'):
                parts.append('金钱 × %d' % reward['cash'])
            if reward.get('binded_cash'):
                parts.append('绑定金钱 × %d' % reward['binded_cash'])
            if reward.get('yuanbao'):
                parts.append('元宝 × %d' % reward['yuanbao'])
            if parts:
                packets.append(notice(session, '任务完成。获得' + '，'.join(parts) + '。', chat=True))
        else:
            packets.append(avatar_client_message(session, 'accept_quest', number))
            for key, value in client_quest_data(character).get(number, {}).items():
                if key != 101:
                    packets.append(avatar_client_message(session, 'update_quest_data', number, key, value))
            from quest_story import acceptance_messages
            packets.extend(acceptance_messages(session, number, character))
    return packets


def activate_idler(session, npc, item_id=None, now=None):
    from battle_service import selected
    character = selected(session)
    if (item_id is None and npc.get('custom_battle')
            and near(session, npc, character)):
        from battle_service import start_idler_battle
        return start_idler_battle(session, npc)
    if not near(session, npc, character) or not eligible(character, npc, item_id):
        return []
    delay = npc.get('intonate', {}).get('time', 0)
    if delay:
        action = character['world_state'].get('quest_action', {})
        if action.get('npc') == npc['entity_id'] and action.get('item') == item_id:
            return []
        def start(c):
            if not eligible(c, npc, item_id):
                return
            c['world_state']['quest_action'] = {
                'npc': npc['entity_id'], 'item': item_id,
                'due': (time.time() if now is None else now) + delay,
                'position': list(session.position),
                'generations': {n: p.get('generation') for n, p in c['tasks'].items() if active(c, int(n))}}
        character_store.transact(session, start)
        return [avatar_client_message(session, 'begin_intonate', max(1, min(255, int(delay))),
                                      npc.get('intonate', {}).get('tips', '正在使用…'))]
    return finish_idler(session, npc, item_id)


def finish_idler(session, npc, item_id=None, expected=None):
    changed, links = [], []
    before = []

    def change(c):
        if expected is not None:
            pending = c['world_state'].pop('quest_action', None)
            if pending != expected or any(c['tasks'].get(n, {}).get('generation') != generation
                                          for n, generation in expected['generations'].items()):
                return
        if not near(session, npc, c) or not eligible(c, npc, item_id):
            return
        before.append(copy.deepcopy(c))
        from quest_service import gain_counters
        if npc.get('gain_counters'):
            changed.extend(gain_counters(c, [npc['gain_counters']]))
        if npc.get('gain_items'):
            changed.extend(grant_objective_items(c, npc['gain_items']))
        if not changed:
            return
        states = c['world_state'].setdefault('quest_states', {})
        for state, level, *_ in npc.get('gain_stats', []):
            states[str(state)] = level
        for identifier, amount in npc.get('revoke_items', []):
            consume(c, identifier, amount)
        from quest_service import objectives_met
        for number in changed:
            c['tasks'][str(number)]['status'] = 'ready' if objectives_met(c, number) else 'active'
        # A recovered actor can stand in for an unavailable server cell
        # script.  The NPC and generation are still validated above.
        from quest_controller import record_interaction
        for number, progress in c.get('tasks', {}).items():
            if active(c, int(number)) and (definition(int(number)) or {}).get('aquire_npcs'):
                if npc['template_id'] in (definition(int(number)) or {}).get('aquire_npcs', []):
                    if record_interaction(c, int(number), npc):
                        changed.append(int(number))
        links.extend(settle_links(c))

    try:
        c = character_store.transact(session, change)
    except ValueError as error:
        character_store.transact(session, lambda c: c['world_state'].pop('quest_action', None))
        return [notice(session, '操作未完成：' + str(error))]
    if not changed:
        return []
    LOG.info('NPC objective committed: template=%d quests=%s', npc['template_id'], changed)
    from quest_service import counter_messages
    from npc_service import update_interest
    from progression_service import messages as progression_messages
    from inventory_service import delta_messages
    from quest_service import item_objective_messages
    packets = delta_messages(session, before[0], c) + item_objective_messages(session, c)
    packets += counter_messages(session, c, changed) + linked_messages(session, c, links)
    if npc.get('script'):
        import uuid
        packets.append(avatar_client_message(session, 'play_script', npc['script'][0], uuid.uuid4().bytes))
    packets += progression_messages(session, c) + update_interest(session)
    if npc['template_id'] == 14096 and active(c, 45206):
        # Server flight choreography is unavailable: use the original landing
        # actor position for a simulator transfer after an actual sword use.
        from npc_service import npc_by_template
        target = npc_by_template(23007, 14, 'QuestMan')
        character_store.update(session, position=list(target['position']))
        from world_service import transfer_scene
        packets += transfer_scene(session)
    return packets


def grant_objective_items(character, gains):
    from inventory_service import grant_items, QUEST_BAG_PAGE
    changed = []
    for identifier, amount in gains:
        needed = [(int(n), count) for n in character['tasks'] if active(character, int(n))
                  for item, count in (definition(int(n)) or {}).get('cq', {}).get('req', {}).get('items', [])
                  if item == identifier]
        limit = max((n for _, n in needed), default=0)
        amount = min(amount, max(0, limit - item_count(character, identifier)))
        if amount:
            grant_items(character, [(identifier, amount)], QUEST_BAG_PAGE)
            changed.extend(n for n, _ in needed)
    return changed


def tick(session, now=None):
    if not session.world_ready or getattr(session, 'battle_id', ''):
        return []
    from battle_service import selected
    from npc_service import find_npc
    c = selected(session)
    action = (c or {}).get('world_state', {}).get('quest_action')
    if not action:
        return []
    moved = sum((session.position[i] - action.get('position', session.position)[i]) ** 2 for i in (0, 2)) > 2 ** 2
    if moved:
        character_store.transact(session, lambda c: c['world_state'].pop('quest_action', None))
        return [avatar_client_message(session, 'stop_intonate')]
    if (time.time() if now is None else now) < action['due']:
        return []
    if action.get('kind') == 'item':
        packets = finish_item(session, action['page'], action['pos'], action['uuid'], action,
                              now=now)
        return [avatar_client_message(session, 'stop_intonate')] + packets
    npc = find_npc(action['npc'])
    if npc and near(session, npc, c):
        packets = finish_idler(session, npc, action['item'], action)
    else:
        character_store.transact(session, lambda c: c['world_state'].pop('quest_action', None))
        packets = []
    return [avatar_client_message(session, 'stop_intonate')] + packets


def consume(character, identifier, amount):
    if item_count(character, identifier) < amount:
        raise ValueError('所需任务物品不足。')
    for item in list(character['inventory']):
        if item['id'] != identifier:
            continue
        removed = min(amount, item['amount'])
        item['amount'] -= removed
        amount -= removed
        if not item['amount']:
            character['inventory'].remove(item)
        if not amount:
            return


def use_quest_item(session, page, pos, target=None):
    from battle_service import selected
    from npc_service import find_npc
    c = selected(session)
    if c is None or not session.world_ready or getattr(session, 'battle_id', ''):
        return True, []
    item = next((i for i in (c or {}).get('inventory', []) if (i['page'], i['pos']) == (page, pos)), None)
    if not item:
        return False, []
    item_data = catalog()['quest_items'].get(str(item['id']), {})
    completing = item_data.get('complete_quest')
    if completing and not active(c, completing):
        return True, [notice(session, '当前任务无需使用该物品。')]
    # ``aquire_item`` quests are offered by consuming the original quest item.
    # Keep the item in the bag until completion when the definition also has a
    # normal inventory objective; otherwise the controller records the source
    # item as the acceptance receipt.
    candidates = [int(n) for n, q in catalog()['quests'].items()
                  if q.get('aquire_item') == item['id'] and not active(c, int(n))
                  and can_accept(c, int(n))]
    if candidates:
        preferred = item_data.get('aquire_quest')
        number = preferred if preferred in candidates else candidates[0]
        from quest_service import accept
        from quest_offline import guide
        return True, accept(session, dict(guide(session), quest_guide=True), number)
    if target is not None:
        npc = find_npc(target)
        if npc and npc['type'] == 'Idler':
            return True, activate_idler(session, npc, item['id'])
        return True, []
    data = catalog()['quest_items'].get(str(item['id']))
    if not data:
        if item['id'] == 20154:
            from summon_service import hatch
            return True, hatch(session, page, pos)
        return False, []
    if not event_supported(data, 'item') and not completing:
        return True, [notice(session, '该任务物品的特殊玩法尚未恢复。')]
    req = data.get('req', {})
    if req.get('quests') and not any(active(c, n) for n in req['quests']):
        return True, [notice(session, '当前任务无需使用该物品。')]
    if req.get('space_no') and c['space_no'] not in values(req['space_no']):
        return True, [notice(session, '请到任务指定地图使用。')]
    if any(item_count(c, i) < amount for i, amount in req.get('items', [])):
        return True, [notice(session, '缺少所需任务物品。')]
    areas = req.get('areas', [])
    if areas and not any(c['space_no'] == a[0] and sum((session.position[i] - a[1][i]) ** 2
                                                     for i in (0, 2)) <= a[2] ** 2 for a in areas):
        return True, [notice(session, '请到任务指定地点使用。')]
    if data.get('intonate', {}).get('time'):
        delay = data['intonate']['time']
        def start(character):
            pending = character['world_state'].get('quest_action', {})
            if pending.get('uuid') == item['uuid']:
                return
            character['world_state']['quest_action'] = {
                'kind': 'item', 'page': page, 'pos': pos, 'uuid': item['uuid'],
                'due': time.time() + delay, 'position': list(session.position),
                'generations': {n: p.get('generation') for n, p in character['tasks'].items()
                                if active(character, int(n))}}
        character_store.transact(session, start)
        return True, [avatar_client_message(session, 'begin_intonate', max(1, min(255, int(delay))),
                                            data['intonate'].get('tips', '正在使用…'))]
    return True, finish_item(session, page, pos, item['uuid'])


def finish_item(session, page, pos, token, expected=None, *, now=None):
    # A complete-item objective is a normal quest settlement whose trigger is
    # the item-use timer.  Keep the item and generation bound to the request,
    # then let quest_service perform the single reward/inventory transaction.
    from battle_service import selected
    current = selected(session)
    item = next((i for i in (current or {}).get('inventory', [])
                 if (i['page'], i['pos'], i['uuid']) == (page, pos, token)), None)
    data = catalog()['quest_items'].get(str(item['id']), {}) if item else {}
    if data.get('complete_quest'):
        if not session.world_ready or getattr(session, 'battle_id', ''):
            return []
        if data.get('intonate', {}).get('time') and (
                not expected or (time.time() if now is None else now) < expected.get('due', float('inf'))):
            return []
        number = int(data['complete_quest'])
        from quest_service import complete
        from quest_offline import guide
        npc = dict(guide(session), quest_guide=True)
        return complete(session, npc, number, item_use={
            'page': page, 'pos': pos, 'uuid': token, 'expected': expected})
    changed, links = [], []
    before, transfer, scripts = [], [], []

    def change(character):
        if expected is not None:
            pending = character['world_state'].pop('quest_action', None)
            if pending != expected or any(character['tasks'].get(n, {}).get('generation') != g
                                          for n, g in expected['generations'].items()):
                return
        item = next((i for i in character['inventory'] if (i['page'], i['pos'], i['uuid'])
                     == (page, pos, token)), None)
        if not item:
            return
        data = catalog()['quest_items'].get(str(item['id']))
        if not data or not event_supported(data, 'item'):
            return
        req = data.get('req', {})
        if req.get('quests') and not any(active(character, n) for n in req['quests']):
            return
        if req.get('space_no') and character['space_no'] not in values(req['space_no']):
            return
        if any(item_count(character, i) < amount for i, amount in req.get('items', [])):
            return
        if req.get('areas') and not any(character['space_no'] == a[0] and
                sum((session.position[i] - a[1][i]) ** 2 for i in (0, 2)) <= a[2] ** 2
                for a in req['areas']):
            return
        before.append(copy.deepcopy(character))
        from quest_service import gain_counters
        gain = data.get('gain', {}).get('counters')
        if gain:
            changed.extend(gain_counters(character, [gain]))
        changed.extend(grant_objective_items(character, data.get('gain', {}).get('items', [])))
        if changed:
            states = character['world_state'].setdefault('quest_states', {})
            for state, level, *_ in data.get('gain', {}).get('stats', []):
                states[str(state)] = level
            if data.get('revoke_req_items'):
                for identifier, amount in req.get('items', []):
                    consume(character, identifier, amount)
            from quest_service import objectives_met
            for number in changed:
                character['tasks'][str(number)]['status'] = 'ready' if objectives_met(character, number) else 'active'
            links.extend(settle_links(character))
            if data.get('script'):
                scripts.append(data['script'][0])
        if data.get('teleport'):
            from npc_service import map_config
            space, position, *_ = data['teleport']
            map_config(space_no=space)
            character.update(space_no=space, position=list(position))
            transfer.append(True)

    try:
        c = character_store.transact(session, change)
    except (ValueError, StopIteration) as error:
        character_store.transact(session, lambda c: c['world_state'].pop('quest_action', None))
        return [notice(session, '操作未完成：' + (str(error) or '目的地暂不可用。'))]
    from quest_service import counter_messages, item_objective_messages
    from inventory_service import delta_messages
    packets = delta_messages(session, before[0], c) if before else []
    packets += counter_messages(session, c, changed) + linked_messages(session, c, links)
    if scripts:
        import uuid
        packets += [avatar_client_message(session, 'play_script', script, uuid.uuid4().bytes) for script in scripts]
    if changed:
        from progression_service import messages as progression_messages
        packets += item_objective_messages(session, c) + progression_messages(session, c)
        from npc_service import update_interest
        packets += update_interest(session)
    if transfer:
        from world_service import transfer_scene
        packets += transfer_scene(session)
    return packets


def talk_quest_npc(session, npc):
    # Original 45210 counter is explicitly "人偶的引荐"; require the
    # actual introduction NPC interaction, rather than acceptance alone.
    from battle_service import selected
    c = selected(session)
    if not near(session, npc, c) or npc['template_id'] != 5268 or not active(c, 45210):
        return []
    changed = []
    def change(c):
        from quest_service import gain_counters
        changed.extend(gain_counters(c, [(6057, 1)]))
    c = character_store.transact(session, change)
    from quest_service import counter_messages
    return counter_messages(session, c, changed)


def monster_victory(character, encounter):
    from quest_service import objectives_met
    changed = []
    for number, progress in character['tasks'].items():
        if not active(character, int(number)):
            continue
        req = (definition(int(number)) or {}).get('cq', {}).get('req', {})
        for field, key, identifier in (('monsters', 3, encounter.get('monster', {}).get('id')),
                                       ('outside_monsters', 4, encounter.get('born_id'))):
            for target, amount in req.get(field, []):
                if target != identifier:
                    continue
                counts = progress.setdefault(field, {})
                counts[str(target)] = min(amount, counts.get(str(target), 0) + 1)
                progress['data'][str(key)] = dict(counts)
                changed.append(int(number))
        if objectives_met(character, int(number)):
            progress['status'] = 'ready'
    return changed
