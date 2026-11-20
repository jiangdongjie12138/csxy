"""Persist the original introductory dialogue and weapon equipment quests."""
import logging
import time
import zlib

import character_store
from entity_methods import avatar_client_message
from quest_catalog import INTRO_QUESTS, definition, supported, can_accept, item_count

LOG = logging.getLogger('quest')
FIRST_QUEST = 45195
WEAPON_QUEST = 45196
ARMOUR_QUEST = 45197
BUTTERFLY_QUEST = 45198
TOUR_QUEST = 45199
SISTERS_QUEST = 45200
SUPPORTED_QUESTS = tuple(sorted(supported()))
WEAPON_COUNTER = 6553
QD_ACCEPT_LV = 0
QD_ACCEPT_TIME = 1
QD_UNREADED = 101
QD_COUNTER = 5
QD_ITEM_CNT = 103
# Original cq.req.items. These are inventory objectives, not kill counters.
ITEM_OBJECTIVES = {n: tuple(tuple(row) for row in definition(n).get('cq', {}).get('req', {}).get('items', []))
                   for n in SUPPORTED_QUESTS
                   if definition(n).get('cq', {}).get('req', {}).get('items')}
SUPPORTED_KEYS = {str(n) for n in SUPPORTED_QUESTS}


class QuestRequestError(ValueError):
    pass


def client_quest_data(character):
    data = {}
    for number, progress in character['tasks'].items():
        if (str(number) in SUPPORTED_KEYS and isinstance(progress, dict)
                and progress.get('status') in ('active', 'ready')):
            values = {int(key): value for key, value in progress['data'].items()}
            for key in (3, 4, QD_COUNTER):
                if key in values:
                    values[key] = {int(k): v for k, v in values[key].items()}
            if int(number) in ITEM_OBJECTIVES:
                # Derive the cache from actual inventory, including old saves
                # with no QD_ITEM_CNT. Never persist a second source of truth.
                values[QD_ITEM_CNT] = {
                    item_id: sum(item['amount'] for item in character['inventory'] if item['id'] == item_id)
                    for item_id, _ in ITEM_OBJECTIVES[int(number)]}
            data[int(number)] = values
    return data


def item_objective_messages(session, character):
    """Send after inventory so the tracker reads the new package count.

    QD_ITEM_CNT is the client's comparison cache and has no refresh signal of
    its own. Reconfirm the unchanged acceptance time as part of this task
    snapshot: QUEST_DATA_ACCEPT_TIME makes QuestTrackDialog reread objectives,
    even if its delayed item observer missed an update during scene changes.
    """
    result = []
    for quest_no, data in client_quest_data(character).items():
        if quest_no in ITEM_OBJECTIVES:
            for key in (QD_ITEM_CNT, QD_ACCEPT_TIME):
                result.append(avatar_client_message(session, 'update_quest_data', quest_no, key, data[key]))
    return result


def resync_item_objectives(session, character):
    """Idempotent quest-item snapshot for settlement or a resumed connection."""
    from inventory_service import item_info, RES_KIND_INV
    data = client_quest_data(character)
    item_ids = {item_id for number in data if number in ITEM_OBJECTIVES
                for item_id, _ in ITEM_OBJECTIVES[number]}
    result = [avatar_client_message(session, 'res_insert', RES_KIND_INV,
                                   item['page'], item['pos'], item_info(item))
              for item in character['inventory'] if item['id'] in item_ids]
    return result + item_objective_messages(session, character)


def client_quest_flags(character):
    # QuestFlagInfo is a compressed bytearray, indexed by quest number.
    completed = [int(number) for number, progress in character['tasks'].items()
                 if str(number) in SUPPORTED_KEYS
                 and isinstance(progress, dict) and progress.get('status') == 'completed']
    flags = bytearray(max(completed, default=-1) // 8 + 1)
    for number in completed:
        flags[number // 8] |= 1 << (number % 8)
    return {'data': zlib.compress(bytes(flags))}


def accept(session, npc, quest_no):
    if getattr(session, 'battle_id', ''):
        return []
    q = definition(quest_no)
    if (quest_no not in SUPPORTED_QUESTS or npc is None
            or (not npc.get('quest_guide') and npc['template_id'] not in q.get('aquire_npcs', []))):
        LOG.info('Quest acceptance unavailable: quest=%d', quest_no)
        return []

    newly_accepted = []
    def change(character):
        if npc.get('space_no', character.get('space_no')) != character.get('space_no'):
            raise QuestRequestError('请在任务发布人所在的地图接取任务。')
        if not can_accept(character, quest_no, linked=bool(npc.get('quest_guide') and q.get('prior_linked_quest'))):
            raise QuestRequestError('尚未满足任务的前置、等级或接取条件。')
        progress = character['tasks'].get(str(quest_no))
        if progress is not None:
            if progress.get('status') in ('active', 'ready'):
                return  # Same application request is safe to confirm again.
            q_repeat = q.get('is_repeatable_branch') or q.get('circle_quest_id') or q.get('times_per_day')
            if not q_repeat:
                raise QuestRequestError('quest already completed')
            history = character.setdefault('world_state', {}).setdefault('quest_history', {}).setdefault(str(quest_no), [])
            history.append({'completed_at': progress.get('completed_at'), 'reward': progress.get('reward', {})})
        initialize(character, quest_no)
        if q.get('aquire_item'):
            source = next(i for i in character['inventory'] if i['id'] == q['aquire_item'])
            character['tasks'][str(quest_no)]['source_item'] = {'id': source['id'], 'uuid': source['uuid']}
            if q['aquire_item'] not in dict(q.get('cq', {}).get('req', {}).get('items', [])):
                from quest_event_service import consume
                consume(character, source['id'], 1)
        newly_accepted.append(True)
        if quest_no == WEAPON_QUEST:
            character['tasks'][str(quest_no)]['data'][str(QD_COUNTER)] = {str(WEAPON_COUNTER): 0}
            from equipment_service import has_intro_weapon
            if has_intro_weapon(character):
                mark_weapon_objective(character)
        elif quest_no == TOUR_QUEST:
            character['tasks'][str(quest_no)].update(tour_next=0, tour_phase='paused')
        elif quest_no == SISTERS_QUEST:
            import uuid
            character['tasks'][str(quest_no)]['dialogue_generation'] = uuid.uuid4().hex
            character['tasks'][str(quest_no)]['data'][str(QD_COUNTER)] = {
                str(counter): 0 for counter in range(6561, 6567)}
        from quest_story import accepted
        accepted(character, quest_no)

    try:
        character = character_store.transact(session, change)
    except QuestRequestError as error:
        LOG.info('Quest acceptance rejected: quest=%d reason=%s', quest_no, error)
        if quest_no <= SISTERS_QUEST:
            return []
        from attribute_service import notice
        return [notice(session, str(error))]
    if character is None:
        return []
    data = client_quest_data(character)[quest_no]
    responses = [avatar_client_message(session, 'accept_quest', quest_no)]
    responses.extend(avatar_client_message(session, 'update_quest_data', quest_no, key, value)
                     for key, value in data.items() if key != QD_UNREADED)
    LOG.info('Quest accepted and saved: quest=%d', quest_no)
    if quest_no >= TOUR_QUEST:
        from npc_service import update_interest
        responses.extend(update_interest(session))
    if quest_no == TOUR_QUEST:
        from tutorial_service import resume_tour
        responses.extend(resume_tour(session))
    from quest_story import acceptance_messages
    if newly_accepted:
        if q.get('aquire_item'):
            from inventory_service import resync_messages
            responses.extend(resync_messages(session, character))
        responses.extend(acceptance_messages(session, quest_no, character))
        from quest_controller import acceptance_messages as controller_messages
        responses.extend(controller_messages(session, quest_no, character))
    return responses


def abandon(session, quest_no):
    if getattr(session, 'battle_id', ''):
        return []
    if quest_no not in SUPPORTED_QUESTS:
        return []
    if definition(quest_no).get('no_abandon'):
        from attribute_service import notice
        return [notice(session, '该任务不能放弃。')]

    def change(character):
        progress = character['tasks'].get(str(quest_no))
        if progress is None or progress.get('status') not in ('active', 'ready'):
            raise QuestRequestError('no active quest to abandon')
        del character['tasks'][str(quest_no)]
        from quest_story import abandoned
        abandoned(character, quest_no)

    try:
        character = character_store.transact(session, change)
    except QuestRequestError:
        return []
    if character is None:
        return []
    LOG.info('Quest abandoned and saved: quest=%d', quest_no)
    responses = [avatar_client_message(session, 'abandon_quest', quest_no)]
    if quest_no == TOUR_QUEST:
        from tutorial_service import clear_tour_messages
        responses.extend(clear_tour_messages(session))
    from npc_service import update_interest
    if quest_no >= TOUR_QUEST:
        responses.extend(update_interest(session))
    return responses


def complete(session, npc, quest_no, selected_item=None, *, item_use=None):
    if getattr(session, 'battle_id', ''):
        return []
    q = definition(quest_no)
    if (quest_no not in SUPPORTED_QUESTS or npc is None
            or (not npc.get('quest_guide') and npc['template_id'] not in q.get('complete_npcs', []))):
        LOG.info('Quest completion unavailable: quest=%d', quest_no)
        return []

    consumed_operations = []
    before_inventory = []
    new_rewards = []
    links = []

    def change(character):
        progress = character['tasks'].get(str(quest_no))
        if progress is None:
            raise QuestRequestError('quest has not been accepted')
        if progress.get('status') == 'completed' and progress.get('rewarded'):
            return  # Confirmation can be retried, reward can only commit once.
        if (progress.get('status') not in ('active', 'ready')
                or npc.get('space_no', character.get('space_no')) != character.get('space_no')):
            raise QuestRequestError('quest is not ready for this NPC')
        try:
            import copy
            before_inventory.append(copy.deepcopy(character))
            if quest_no == TOUR_QUEST and progress.get('tour_phase') != 'arrived':
                raise QuestRequestError('请先与梦珠一起游览极乐村，再找玉珠。')
            if quest_no == SISTERS_QUEST and any(
                    progress['data'].get(str(QD_COUNTER), {}).get(str(counter), 0) < 1
                    for counter in range(6561, 6567)):
                raise QuestRequestError('请先与六位姐妹分别对话，再找梦珠。')
            if q.get('complete_item'):
                if not item_use:
                    raise QuestRequestError('请使用指定任务物品完成任务。')
                page, pos = item_use['page'], item_use['pos']
                item = next((i for i in character['inventory']
                             if (i['page'], i['pos'], i['uuid']) == (page, pos, item_use['uuid'])), None)
                if not item or item['id'] != int(q['complete_item']):
                    raise QuestRequestError('请使用指定任务物品完成任务。')
                from quest_catalog import catalog
                item_data = catalog()['quest_items'].get(str(item['id']), {})
                areas = item_data.get('req', {}).get('areas', [])
                if areas and not any(character['space_no'] == area[0] and
                        sum((session.position[i] - area[1][i]) ** 2 for i in (0, 2)) <= area[2] ** 2
                        for area in areas):
                    raise QuestRequestError('请到任务指定地点使用。')
                expected = item_use.get('expected')
                if item_data.get('intonate', {}).get('time'):
                    pending = character['world_state'].get('quest_action', {})
                    if (not expected or pending != expected or pending.get('uuid') != item['uuid']
                            or pending.get('generations', {}).get(str(quest_no)) != progress.get('generation')
                            or sum((session.position[i] - pending['position'][i]) ** 2 for i in (0, 2)) > 4):
                        raise QuestRequestError('任务物品使用状态已过期，请重新使用。')
                    character['world_state'].pop('quest_action', None)
                progress['item_used'] = True
            if not objectives_met(character, quest_no):
                raise QuestRequestError('任务目标尚未完成，请查看任务追踪。')
            for item_id, amount in ITEM_OBJECTIVES.get(quest_no, []):
                # The client counts the original quest item directly. Remove it
                # atomically with completion and rewards, never on preview.
                remaining = amount
                for item in list(character['inventory']):
                    if item['id'] == item_id:
                        consumed = min(remaining, item['amount'])
                        item['amount'] -= consumed
                        remaining -= consumed
                        if not item['amount']:
                            character['inventory'].remove(item)
                            consumed_operations.append(('res_remove',0,item['page'],item['pos']))
                        else:
                            consumed_operations.append(('res_wrap',0,item['page'],item['pos'],item['amount']))
                        if not remaining:
                            break
            from quest_controller import consume_extra
            consume_extra(character, quest_no, selected_item)
            from inventory_service import grant_reward
            reward = grant_reward(character, quest_no)
            from quest_story import completed
            completed(character, quest_no, reward)
            # Award simulator XP only on first completion, in the same commit.
            from progression_service import grant_quest_experience
            reward.update(grant_quest_experience(character, q))
        except ValueError as error:
            raise QuestRequestError(str(error)) from error
        progress.update(status='completed', completed_at=int(time.time()), rewarded=True, reward=reward)
        from quest_controller import completed as controller_completed
        controller_completed(character, quest_no)
        new_rewards.append(reward)
        from quest_event_service import settle_links
        try:
            links.extend(settle_links(character))
        except ValueError as error:
            raise QuestRequestError(str(error)) from error

    try:
        character = character_store.transact(session, change)
    except QuestRequestError as error:
        LOG.info('Quest completion rejected: quest=%d reason=%s', quest_no, error)
        from inventory_service import InventoryFullError
        if isinstance(error.__cause__, InventoryFullError):
            from attribute_service import notice
            return [notice(session, '背包空间不足，请先整理背包后再提交任务。')]
        if quest_no <= BUTTERFLY_QUEST:
            return []
        from attribute_service import notice
        return [notice(session, str(error))]
    if character is None:
        return []
    from inventory_service import messages
    responses = messages(session, character)
    # The original tutorial chain (45195-45221) already sends the complete
    # inventory snapshot from ``messages``.  Adding a delta snapshot here
    # duplicates every item packet and makes a retry produce a different wire
    # response from the first completion.  Keep the legacy packet contract
    # for that chain; generic and restored quests still receive the delta so
    # consumed/reward items are visible immediately.
    legacy_intro = FIRST_QUEST <= quest_no <= 45221
    if before_inventory and not legacy_intro:
        from inventory_service import delta_messages
        responses = delta_messages(session, before_inventory[0], character) + responses
    responses = [avatar_client_message(session,name,*args) for name,*args in consumed_operations] + responses
    if new_rewards:
        from attribute_service import notice
        from inventory_service import intro_reward
        reward = new_rewards[0]
        parts = ['金钱 × %d' % reward['cash']] if reward.get('cash') else []
        if reward.get('binded_cash'):
            parts.append('绑定金钱 × %d' % reward['binded_cash'])
        if reward.get('yuanbao'):
            parts.append('元宝 × %d' % reward['yuanbao'])
        if reward.get('experience'):
            parts.append('经验 × %d' % reward['experience'])
        parts.extend('%s × %d' % (intro_reward()['items'][str(item_id)]['name'], amount)
                     for item_id, amount in reward.get('items', []))
        if parts:
            responses.append(notice(session, '任务完成。获得' + '，'.join(parts) + '。', chat=True))
    responses.append(avatar_client_message(session, 'update_quest_flag', quest_no, 1))
    if quest_no == TOUR_QUEST:
        from tutorial_service import clear_tour_messages
        responses.extend(clear_tour_messages(session))
    from progression_service import messages as progression_messages
    responses.extend(progression_messages(session, character))
    from npc_service import update_interest
    if quest_no >= TOUR_QUEST:
        responses.extend(update_interest(session))
    responses.append(avatar_client_message(session, 'finish_quest', quest_no, npc['entity_id']))
    if links:
        from quest_event_service import linked_messages
        responses.extend(linked_messages(session, character, links))
    LOG.info('Quest completed and rewards saved: quest=%d reward_items=%d', quest_no,
             len(character['tasks'][str(quest_no)]['reward']['item_uuids']))
    return responses


def initialize(character, number):
    import uuid
    now = int(time.time())
    character['tasks'][str(number)] = {
        'status': 'active', 'accepted_at': now, 'accepted_level': character['level'],
        'generation': uuid.uuid4().hex,
        'data': {str(QD_ACCEPT_LV): character['level'], str(QD_ACCEPT_TIME): now,
                 str(QD_UNREADED): True}}
    req = definition(number).get('cq', {}).get('req', {})
    counters = req.get('counters', [])
    if counters:
        character['tasks'][str(number)]['data'][str(QD_COUNTER)] = {str(row[0]): 0 for row in counters}
    if definition(number).get('custom_battle'):
        character['tasks'][str(number)]['data']['2'] = False
    from quest_controller import initialize as initialize_controller
    initialize_controller(character, number)


def objectives_met(character, number):
    progress = character['tasks'].get(str(number), {})
    if progress.get('status') not in ('active', 'ready'):
        return False
    if number == TOUR_QUEST and progress.get('tour_phase') != 'arrived':
        return False
    from quest_story import objectives_met as story_met
    if not story_met(character, number, progress):
        return False
    req = definition(number).get('cq', {}).get('req', {})
    counters = progress['data'].get(str(QD_COUNTER), {})
    if any(counters.get(str(row[0]), 0) < row[1] for row in req.get('counters', [])):
        return False
    if any(item_count(character, row[0]) < row[1] for row in req.get('items', [])):
        return False
    for field in ('monsters', 'outside_monsters'):
        if any(progress.get(field, {}).get(str(row[0]), 0) < row[1] for row in req.get(field, [])):
            return False
    if character['level'] < req.get('level', 0):
        return False
    # Offline companion slots satisfy the original team-size requirement.
    states = character['world_state'].get('quest_states', {})
    if any(states.get(str(row[0] if isinstance(row, list) else row), 0) <
           (row[1] if isinstance(row, list) else 1) for row in req.get('stats', [])):
        return False
    if req.get('achieve') and any(n not in character['world_state'].get('achievements', []) for n in req['achieve']):
        return False
    from quest_controller import objectives_met as controller_objectives
    return controller_objectives(character, number, progress)


def gain_counters(character, gains):
    """Apply only server-validated event gains to relevant active objectives."""
    changed = []
    for number, progress in character['tasks'].items():
        if progress.get('status') not in ('active', 'ready') or int(number) not in SUPPORTED_QUESTS:
            continue
        needed = dict(definition(int(number)).get('cq', {}).get('req', {}).get('counters', []))
        counters = progress['data'].setdefault(str(QD_COUNTER), {}) if needed else {}
        updated = False
        for counter, amount in gains:
            if counter in needed and counters.get(str(counter), 0) < needed[counter]:
                counters[str(counter)] = min(needed[counter], counters.get(str(counter), 0) + amount)
                updated = True
        if updated:
            progress['status'] = 'ready' if objectives_met(character, int(number)) else 'active'
            changed.append(int(number))
    return changed


def counter_messages(session, character, changed):
    packets = []
    for n in dict.fromkeys(changed):
        for key in (2, 3, 4, 5):
            value = character['tasks'][str(n)]['data'].get(str(key))
            if value is not None:
                packets.append(avatar_client_message(session, 'update_quest_data', n, key,
                    {int(k): v for k, v in value.items()} if isinstance(value, dict) else value))
    return packets


def mark_weapon_objective(character):
    """Called inside the equipment/acceptance transaction, after validation."""
    progress = character['tasks'].get(str(WEAPON_QUEST))
    if progress is None or progress.get('status') not in ('active', 'ready'):
        return False
    counters = progress['data'].setdefault(str(QD_COUNTER), {})
    if counters.get(str(WEAPON_COUNTER), 0) >= 1:
        return False
    counters[str(WEAPON_COUNTER)] = 1
    progress['status'] = 'ready'
    return True


def weapon_objective_message(session, character):
    data = client_quest_data(character)[WEAPON_QUEST][QD_COUNTER]
    return avatar_client_message(session, 'update_quest_data', WEAPON_QUEST, QD_COUNTER, data)
