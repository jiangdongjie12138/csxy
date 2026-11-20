"""Generic controllers for non-retired quests whose original cell scripts are absent.

The controller is deliberately event based: accepting a task records a
generation and its requirements; battles, NPC interactions, item use and
state changes produce receipts bound to that generation.  It never sets a
counter merely because a task was accepted.
"""
import functools
import random
import time

from quest_catalog import catalog, definition, active, item_count


@functools.lru_cache(maxsize=1)
def controllers():
    import json
    from csxy_protocol import ROOT
    return json.loads((ROOT / 'server/data/quest_controllers.json').read_text('utf-8'))


def auxiliary():
    return controllers()['tables']


def plans():
    return controllers()['quests']


def plan(number):
    return plans().get(str(number), {})


def acceptance_messages(session, number, character):
    """Hook reserved for controller-specific native packets.

    Generic controllers use the normal questData snapshot; specialized story
    messages remain in quest_story.acceptance_messages.
    """
    return []


def _templates():
    from inventory_service import intro_reward
    return intro_reward()['items']


def _name(item):
    return str(item.get('name', ''))


def count_named(character, names, quality=None, color=None):
    names = set(names or ())
    total = 0
    for item in character.get('inventory', []):
        template = _templates().get(str(item['id']), {})
        if _name(template) not in names:
            continue
        if quality is not None and item.get('attributes', {}).get('quality', template.get('quality', 1)) < quality:
            continue
        if color is not None and item.get('attributes', {}).get('color', template.get('color', 0)) != color:
            continue
        total += item['amount']
    return total


def count_type(character, type_id):
    total = 0
    for item in character.get('inventory', []):
        template = _templates().get(str(item['id']), {})
        if matches_category(item, type_id):
            total += item['amount']
    return total


def matches_category(item, identifier):
    category = auxiliary()['ring_quest_item_type'].get(str(identifier), {})
    template = _templates().get(str(item['id']), {})
    if not category:
        return False
    if category.get('legend_item_id') == item['id']:
        return True
    for key in ('type', 'stype', 'vlv'):
        if key in category and template.get(key) != category[key]:
            return False
    if template.get('lv', 0) < category.get('lv', 0):
        return False
    if 'color' in category and item.get('attributes', {}).get('color', template.get('color', 0)) != category['color']:
        return False
    return True


def _random_item(req):
    """Resolve an original random item category to a deterministic item.

    The category tables contain legend item IDs, which are the same IDs the
    client uses in its tracker.  If a category has no legend item, a plain
    low-level medicine is used as the simulator supply.
    """
    categories = auxiliary()['ring_quest_item_type']
    value = req[0] if isinstance(req, (list, tuple)) else req
    row = categories.get(str(value), {})
    candidates = sorted(int(n) for n, template in _templates().items()
                        if matches_category({'id': int(n), 'attributes': {}}, value)
                        and not template.get('is_quest'))
    if candidates:
        return random.choice(candidates)
    # The original server random-item libraries are absent. Categories not
    # present in ring_quest_item_type use an explicit supply fallback.
    identifier = int(row.get('legend_item_id') or (23023 if value == 1 else 23001))
    return identifier if str(identifier) in _templates() else 23001


def initialize(character, number):
    q = definition(number) or {}
    req = q.get('cq', {}).get('req', {})
    progress = character['tasks'].get(str(number))
    if not progress:
        return
    random_rows = req.get('random_items', [])
    if random_rows:
        resolved = []
        for row in random_rows:
            amount = int(row[1]) if isinstance(row, (list, tuple)) and len(row) > 1 else 1
            resolved.append([_random_item(row), amount])
        progress['random_items'] = resolved
        progress['data']['18'] = resolved
    if q.get('quiz1') or q.get('quiz2'):
        progress['quiz'] = {'answered': False, 'correct': 0, 'asked_at': 0}
    if q.get('circle_quest_id'):
        row = circle_state(character, q['circle_quest_id'])
        progress['circle'] = {'lib': q['circle_quest_id'], 'ring': row.get('ring', 0)}


def _item_objective_met(character, progress, req):
    for identifier, amount in req.get('items', []):
        if item_count(character, identifier) < amount:
            return False
    for identifier, amount in progress.get('random_items', []):
        if item_count(character, identifier) < amount:
            return False
    by_name = req.get('revoke_item_by_name', {})
    if by_name and count_named(character, by_name.get('names'), by_name.get('quality'), by_name.get('color')) < 1:
        return False
    if req.get('revoke_item_by_type') is not None and count_type(character, req['revoke_item_by_type']) < 1:
        return False
    return True


def objectives_met(character, number, progress):
    q = definition(number) or {}
    req = q.get('cq', {}).get('req', {})
    if not _item_objective_met(character, progress, req):
        return False
    counters = progress.get('data', {}).get('5', {})
    if any(counters.get(str(identifier), 0) < amount for identifier, amount in req.get('counters', [])):
        return False
    if any(progress.get('monsters', {}).get(str(identifier), 0) < amount
           for identifier, amount in req.get('monsters', [])):
        return False
    if any(progress.get('outside_monsters', {}).get(str(identifier), 0) < amount
           for identifier, amount in req.get('outside_monsters', [])):
        return False
    if req.get('stats'):
        states = character.get('world_state', {}).get('quest_states', {})
        if any(states.get(str(row[0] if isinstance(row, list) else row), 0) <
               (row[1] if isinstance(row, list) else 1) for row in req['stats']):
            return False
    if req.get('achieve'):
        achievements = set(character.get('world_state', {}).get('achievements', []))
        if any(identifier not in achievements for identifier in req['achieve']):
            return False
    if q.get('custom_battle') and not progress.get('battle_won'):
        return False
    if (q.get('timer_escort') or q.get('escort_man')) and not progress.get('escort_completed') and str(number) not in catalog()['simulator_rules'].get('arrival_quests', {}):
        return False
    if (q.get('quiz1') or q.get('quiz2')) and not progress.get('quiz', {}).get('correct'):
        return False
    if q.get('complete_item') and not progress.get('item_used'):
        return False
    deadline = req.get('time')
    if deadline and time.time() - progress.get('accepted_at', time.time()) > deadline:
        return False
    return True


def _item_quality(character, ranges):
    for item in character.get('inventory', []):
        template = _templates().get(str(item['id']), {})
        quality = template.get('quality', template.get('color', 0))
        for low, high, _factor in ranges:
            if low <= quality <= high:
                yield True


def _has_forge_receipt(progress):
    return bool(progress.get('forge_receipt'))


def record_interaction(character, number, npc=None):
    """Record one unavailable dialogue action; preserve recovered objectives."""
    progress = character['tasks'].get(str(number))
    if not progress or not active(character, number):
        return False
    q = definition(number) or {}
    if q.get('quiz1') or q.get('quiz2'):
        progress.setdefault('quiz', {'answered': False, 'correct': 0, 'asked_at': 0})['asked_at'] = int(time.time())
        return False
    req = q.get('cq', {}).get('req', {})
    if not npc or npc.get('template_id') not in q.get('complete_npcs', []):
        return False
    counters = progress['data'].setdefault('5', {})
    for step in plan(number).get('steps', []):
        if step['kind'] == 'interact':
            identifier = str(step['counter'])
            before = counters.get(identifier, 0)
            if before < step['amount']:
                counters[identifier] = min(step['amount'], before + 1)
                return True
    return False


def answer_quiz(character, number, option):
    progress = character['tasks'].get(str(number))
    q = definition(number) or {}
    if not progress or not active(character, number) or not (q.get('quiz1') or q.get('quiz2')):
        return False
    quiz_id = progress.get('quiz', {}).get('question_id') or q.get('quiz1') or q.get('quiz2')
    table = auxiliary()['wen_question' if q.get('quiz2') else 'keju_question']
    row = table.get(str(quiz_id)) or table.get(str(quiz_id + 1))
    correct = row.get('answer', 0) if row else 0
    quiz = progress.setdefault('quiz', {'answered': False, 'correct': 0, 'asked_at': 0})
    quiz['answered'] = True
    quiz['correct'] = int(option == correct)
    if quiz['correct']:
        req = q.get('cq', {}).get('req', {})
        for identifier, amount in req.get('counters', []):
            progress['data'].setdefault('5', {})[str(identifier)] = amount
        progress['status'] = 'ready' if objectives_met(character, number, progress) else 'active'
    return bool(quiz['correct'])


def battle_event(character, encounter):
    """Supply unresolved quest sources after a committed victory.

    Each generated item is capped by the active task's requirement and is
    tagged to its generation for idempotent battle retries.
    """
    changed = []
    number = encounter.get('quest', 0)
    progress = character.get('tasks', {}).get(str(number), {})
    if (not number or not active(character, number)
            or encounter.get('generation') != progress.get('generation')):
        return changed
    if encounter.get('objective'):
        apply_action(character, number, encounter['objective'])
        changed.append(number)
    if encounter.get('custom_id'):
        from quest_service import gain_counters
        for battle in battle_chain(encounter['custom_id']):
            gain = battle.get('reward', {}).get('counters')
            if gain:
                changed.extend(gain_counters(character, [gain]))
            from quest_event_service import grant_objective_items
            changed.extend(grant_objective_items(character, [[r[0], r[1]] for r in battle.get('items', [])]))
        if (definition(number) or {}).get('custom_battle') == encounter['custom_id']:
            progress['battle_won'] = True
            progress['data']['2'] = True
            changed.append(number)
    return list(dict.fromkeys(changed))


def battle_chain(identifier):
    pending, visited, result = [identifier], set(), []
    while pending and len(visited) < 32:
        number = pending.pop(0)
        if number in visited:
            continue
        visited.add(number)
        row = catalog()['battles'].get(str(number), {})
        result.append(row)
        pending.extend(row.get('next_battle', []))
    return result


def item_requirements(character, number):
    progress = character['tasks'].get(str(number), {})
    return list((definition(number) or {}).get('cq', {}).get('req', {}).get('items', [])) + progress.get('random_items', [])


def consume_extra(character, number, selected=None):
    """Consume only the validated task material inside reward transaction."""
    from quest_event_service import consume
    progress = character['tasks'][str(number)]
    for identifier, amount in progress.get('random_items', []):
        consume(character, identifier, amount)
    req = definition(number).get('cq', {}).get('req', {})
    by_name = req.get('revoke_item_by_name', {})
    type_id = req.get('revoke_item_by_type')
    if by_name or type_id:
        candidates = [i for i in character['inventory']
                      if (not selected or (i['page'], i['pos']) == selected)
                      and (not by_name or named_match(i, by_name))
                      and (type_id is None or matches_category(i, type_id))]
        if not candidates:
            raise ValueError('提交的物品不符合名称、品质或类型要求。')
        item = candidates[0]
        from copy import deepcopy
        progress['submitted_item'] = {'id': item['id'], 'uuid': item['uuid'],
                                      'attributes': deepcopy(item['attributes'])}
        # Two stacks of the same template can have different quality. Consume
        # the selected instance rather than the first matching template ID.
        item['amount'] -= 1
        if not item['amount']:
            character['inventory'].remove(item)


def completed(character, number):
    q = definition(number) or {}
    if not (q.get('circle_quest_id') or q.get('is_repeatable_branch') or q.get('times_per_day')):
        return
    identifier = q.get('circle_quest_id')
    if identifier:
        row = circle_state(character, identifier)
        if q.get('is_ring_end') or not (q.get('is_ring_begin') or q.get('req', {}).get('done_quest_or')):
            row['ring'] += 1
            row['done_today'] += 1
            per_round = max(1, auxiliary()['circle_quest_lib'].get(str(identifier), {}).get('ring_per_round', 1))
            row['round'] = row['done_today'] // per_round
            row['ring'] %= per_round


def circle_state(character, identifier):
    import datetime
    day = datetime.datetime.now().date().isoformat()
    row = character.setdefault('world_state', {}).setdefault('circle_quests', {}).setdefault(
        str(identifier), {'ring': 0, 'round': 0, 'done_today': 0, 'date': day})
    if row.get('date') != day:
        row.update(date=day, ring=0, round=0, done_today=0)
    return row


def repetition_allowed(character, number):
    import datetime
    q = definition(number) or {}
    day = datetime.datetime.now().date().isoformat()
    if q.get('times_per_day'):
        rows = list(character.get('world_state', {}).get('quest_history', {}).get(str(number), []))
        rows.append(character.get('tasks', {}).get(str(number), {}))
        count = sum(1 for p in rows if p.get('completed_at') and
                    datetime.datetime.fromtimestamp(p['completed_at']).date().isoformat() == day)
        if count >= int(q['times_per_day']):
            return False
    identifier = q.get('circle_quest_id')
    if identifier:
        lib = auxiliary()['circle_quest_lib'].get(str(identifier), {})
        if lib.get('shuted') or character['level'] < lib.get('min_lv', 1):
            return False
        if q.get('is_ring_begin') and any(active(character, int(n)) and
                (definition(int(n)) or {}).get('circle_quest_id') == identifier
                for n in character.get('tasks', {})):
            return False
        row = character.get('world_state', {}).get('circle_quests', {}).get(str(identifier), {})
        if (row.get('date') == day and lib.get('round_per_day') and
                row.get('done_today', 0) >= lib['round_per_day'] * max(1, lib.get('ring_per_round', 1))):
            return False
        for other in lib.get('req', {}).get('mutex', []):
            if any(active(character, int(n)) and (definition(int(n)) or {}).get('circle_quest_id') == other
                   for n in character.get('tasks', {})):
                return False
    return True


def named_match(item, req):
    template = _templates().get(str(item['id']), {})
    return (template.get('name') in req.get('names', [])
            and item['attributes'].get('quality', template.get('quality', 1)) >= req.get('quality', 1)
            and ('color' not in req or item['attributes'].get('color', template.get('color', 0)) == req['color']))


def reward_multiplier(character, number):
    req = definition(number).get('cq', {}).get('req', {})
    submitted = character['tasks'].get(str(number), {}).get('submitted_item', {})
    attributes = submitted.get('attributes', {})
    template = _templates().get(str(submitted.get('id')), {})
    multiplier = 1.0
    if attributes.get('maker') or attributes.get('forge'):
        multiplier = max(multiplier, req.get('item_forge_reward', 1.0))
    for low, high, factor in req.get('item_quality_reward', []):
        if low <= attributes.get('quality', template.get('quality', 1)) <= high:
            multiplier = max(multiplier, factor)
    return multiplier


def apply_action(character, number, action):
    """Apply exactly one server-selected objective action to one generation."""
    if not active(character, number):
        raise ValueError('任务已经结束。')
    progress = character['tasks'][str(number)]
    req = definition(number).get('cq', {}).get('req', {})
    kind, identifier = action['kind'], action.get('id')
    if kind == 'counter':
        limit = dict(req.get('counters', [])).get(identifier, 0)
        counts = progress['data'].setdefault('5', {})
        counts[str(identifier)] = min(limit, counts.get(str(identifier), 0) + 1)
    elif kind in ('monsters', 'outside_monsters'):
        limit = dict(req.get(kind, [])).get(identifier, 0)
        counts = progress.setdefault(kind, {})
        counts[str(identifier)] = min(limit, counts.get(str(identifier), 0) + 1)
        progress['data']['3' if kind == 'monsters' else '4'] = dict(counts)
    elif kind == 'item':
        limit = dict(item_requirements(character, number)).get(identifier, 0)
        amount = min(1, max(0, limit - item_count(character, identifier)))
        if amount:
            from inventory_service import grant_items, QUEST_BAG_PAGE
            template = _templates()[str(identifier)]
            grant_items(character, [(identifier, amount)], QUEST_BAG_PAGE if template.get('is_quest') else 0)
    elif kind == 'named_item':
        rule = req.get('revoke_item_by_name', {})
        candidates = sorted(int(n) for n, row in _templates().items() if row.get('name') in rule.get('names', []))
        if not candidates:
            raise ValueError('未找到原始任务物品模板。')
        from inventory_service import grant_items
        grant_items(character, [(candidates[0], 1)], properties={'quality': rule.get('quality', 1), 'color': rule.get('color', 0)})
    elif kind == 'typed_item':
        category = req.get('revoke_item_by_type')
        candidates = sorted(int(n) for n in _templates()
                            if matches_category({'id': int(n), 'attributes': {}}, category))
        if not candidates:
            raise ValueError('没有可用的原始任务材料模板。')
        from inventory_service import grant_items
        grant_items(character, [(candidates[0], 1)])
    elif kind == 'state':
        limits = {str(r[0] if isinstance(r, list) else r): r[1] if isinstance(r, list) else 1 for r in req.get('stats', [])}
        character['world_state'].setdefault('quest_states', {})[str(identifier)] = limits.get(str(identifier), 1)
    elif kind == 'achievement':
        if identifier in req.get('achieve', []):
            achievements = character['world_state'].setdefault('achievements', [])
            if identifier not in achievements:
                achievements.append(identifier)
    elif kind == 'escort':
        progress['escort_completed'] = True
        progress['data']['2'] = True
    else:
        raise ValueError('未知的任务行动。')
    progress.setdefault('receipts', []).append({'kind': kind, 'id': identifier, 'at': int(time.time())})
    progress['status'] = 'ready' if objectives_met(character, number, progress) else 'active'
    return number

