"""Recovered quest definitions and server-side eligibility checks."""
import functools
import json

import character_store
from csxy_protocol import ROOT


@functools.lru_cache(maxsize=1)
def catalog():
    return json.loads((ROOT / 'server/data/quest_system.json').read_text('utf-8'))


def definition(number):
    return catalog()['quests'].get(str(number))


def event_supported(data, kind):
    """Only literal, deterministic events can advance general quests."""
    if kind == 'idler':
        req = data.get('req', {})
        size = req.get('team_size', [1, 5])
        if isinstance(size, list) and not size[0] <= 1 <= size[1]:
            return False
        return not (data.get('custom_battle') or req.get('teamwork') or
                    set(req) - {'quests', 'items', 'use_item', 'min_lv', 'max_lv',
                                'team_size', 'stat', 'lv_tips'})
    allowed = {'id', 'req', 'gain', 'intonate', 'opera', 'script', 'block_counter',
               'revoke_req_items', 'teleport', 'teammember_level', 'show_effect', 'show_pic_id'}
    return not set(data) - allowed


@functools.lru_cache(maxsize=1)
def event_sources():
    from npc_service import entity_index
    npcs = list(entity_index().values())
    monster_models = {(n['aspect'], bytes.fromhex(n['name_gbk']).decode('gbk'))
                      for n in npcs if n['type'] == 'Monster'}
    counters = {6553, 6057, 6560, 6055} | set(range(6561, 6567))
    items = {58081, 58082, 58083, 74083, 74084, 74085, 59588, 74090, 74091}
    items.update(row[0] for rows in catalog()['simulator_rules'].get('acceptance_items', {}).values() for row in rows)
    states = set()
    for n in npcs:
        if n['type'] != 'Idler' or not event_supported(n, 'idler'):
            continue
        if n.get('gain_counters'):
            counters.add(n['gain_counters'][0])
        items.update(row[0] for row in n.get('gain_items', []))
        states.update(row[0] for row in n.get('gain_stats', []))
    for item in catalog()['quest_items'].values():
        if event_supported(item, 'item'):
            gain = item.get('gain', {})
            if gain.get('counters'):
                counters.add(gain['counters'][0])
            items.update(row[0] for row in gain.get('items', []))
            states.update(row[0] for row in gain.get('stats', []))
    items.update(row[2] for q in catalog()['quests'].values()
                 for row in q.get('cq', {}).get('reward', {}).get('items', []))
    # Several fixed story encounters award a quest counter/item directly from
    # the custom battle table.  They have no Idler record to contribute the
    # source, but the simulator validates and applies those rewards itself.
    for rule in catalog()['simulator_rules'].get('custom_battles', {}).values():
        if rule.get('counter'):
            counters.add(int(rule['counter']))
        items.update(int(row[0]) for row in rule.get('items', []))
    return {'counters': counters, 'items': items, 'states': states,
            'questmen': {n['template_id'] for n in npcs if n['type'] == 'QuestMan'},
            'born': {n['template_id'] for n in npcs if n['type'] == 'Monster'},
            'monsters': {int(k) for k, m in catalog()['monsters'].items()
                         if (m.get('aspect'), m.get('name')) in monster_models}}


def unsupported(number):
    q = definition(number)
    if not q:
        return ['missing definition']
    reasons = []
    if q.get('shuted'):
        reasons.append('retired')
    if q.get('circle_quest_id'):
        reasons.append('circle scheduler')
    if (q.get('timer_escort') or q.get('escort_man')) and str(number) not in catalog()['simulator_rules'].get('arrival_quests', {}):
        reasons.append('escort controller')
    custom_battles = catalog()['simulator_rules'].get('custom_battles', {})
    if q.get('custom_battle') and (number not in catalog()['simulator_rules']['story_battles']
                                   and str(number) not in custom_battles):
        reasons.append('custom encounter controller')
    for field in ('aq', 'aquire_item', 'complete_item', 'is_repeatable_branch',
                  'quest_open_time', 'quest_close_time'):
        if q.get(field):
            reasons.append('controller:' + field)
    req = q.get('cq', {}).get('req', {})
    allowed = {'items', 'counters', 'monsters', 'outside_monsters', 'level',
               'max_teamsize', 'min_teamsize', 'teamwork'}
    reasons.extend('objective:' + key for key in q.get('cq', {}).get('req', {}) if key not in allowed)
    reasons.extend('reward:' + key for key in q.get('cq', {}).get('reward', {})
                   if key not in ('items', 'cash', 'binded_cash'))
    if q.get('quiz1') or q.get('quiz2'):
        reasons.append('quiz controller')
    if req.get('min_teamsize', 1) > 1 or q.get('req', {}).get('min_teamsize', 1) > 1:
        reasons.append('team controller')
    sources = event_sources()
    for field, source in (('counters', 'counters'), ('items', 'items'),
                          ('monsters', 'monsters'), ('outside_monsters', 'born')):
        if any(row[0] not in sources[source] for row in req.get(field, [])):
            reasons.append('unrecovered source:' + field)
    for field in ('aquire_npcs', 'complete_npcs'):
        if q.get(field) and not any(n in sources['questmen'] for n in q[field]):
            reasons.append('missing actor:' + field)
    if not q.get('aquire_npcs') and not q.get('prior_linked_quest'):
        reasons.append('missing acquisition event')
    return reasons


@functools.lru_cache(maxsize=1)
def supported():
    # The client carries the complete quest table.  A missing server script is
    # handled by the offline controller catalogue, while the original
    # ``shuted`` flag remains the only hard closure.  ``unsupported`` is kept
    # for audit/provenance and diagnostics.
    return frozenset(int(n) for n, q in catalog()['quests'].items() if not q.get('shuted'))


def active(character, number):
    return character.get('tasks', {}).get(str(number), {}).get('status') in ('active', 'ready')


def done(character, number):
    return character.get('tasks', {}).get(str(number), {}).get('status') == 'completed'


def item_count(character, identifier):
    return sum(i['amount'] for i in character['inventory'] if i['id'] == identifier)


def values(value):
    return value if isinstance(value, (list, tuple)) else [value]


def requirements_met(character, req):
    level = character['level']
    school, sex = bytes.fromhex(character['physique'])[:2]
    if level < req.get('min_lv', 1) or level > req.get('max_lv', 255):
        return False
    if req.get('sch') and school not in values(req['sch']):
        return False
    if req.get('sex') and sex not in values(req['sex']):
        return False
    # The local simulator represents absent party members with companion
    # slots.  Team size is enforced by the controller at the objective stage,
    # so solo acceptance of an original party task is still possible.
    for key, check in (('done_quest_and', done), ('has_quest_and', active)):
        if any(not check(character, n) for n in req.get(key, [])):
            return False
    for key, check in (('done_quest_or', done), ('has_quest_or', active)):
        if req.get(key) and not any(check(character, n) for n in req[key]):
            return False
    if any(active(character, n) or done(character, n) for n in req.get('mutex', [])):
        return False
    if any(item_count(character, row[0]) < row[1] for row in req.get('items', [])):
        return False
    states = character.get('world_state', {}).get('quest_states', {})
    stat_rows = req.get('stats', [])
    if stat_rows and isinstance(stat_rows, (list, tuple)) and stat_rows and not isinstance(stat_rows[0], (list, tuple)):
        stat_rows = [(row, 1) for row in stat_rows]
    if any(states.get(str(row[0]), 0) < row[1] for row in stat_rows):
        return False
    renown = character.get('world_state', {}).get('renown', {})
    renown_rows = req.get('renown_level', [])
    if renown_rows and isinstance(renown_rows, (list, tuple)) and renown_rows and not isinstance(renown_rows[0], (list, tuple)):
        renown_rows = [renown_rows]
    if any(renown.get(str(row[0]), 0) < row[1] for row in renown_rows):
        return False
    known = {'min_lv', 'max_lv', 'sch', 'sex', 'min_teamsize', 'max_teamsize',
             'done_quest_and', 'done_quest_or', 'has_quest_and', 'has_quest_or',
             'mutex', 'items', 'stats', 'renown_level', 'ignore_sch_state',
             'clan', 'equipment', 'times_per_day', 'quest_open_time',
             'quest_close_time', 'same_ring', 'teamwork'}
    # Unknown acceptance metadata belongs to the original server (for example
    # clan/ring state).  It is retained in the definition and the controller
    # records it, but must not turn a live non-retired quest into “暂未开放”.
    return True


def can_accept(character, number, linked=False):
    q = definition(number)
    if number not in supported() or not q:
        return False
    progress = character['tasks'].get(str(number), {})
    if progress:
        if progress.get('status') in ('active', 'ready'):
            return True
        # Branch/circle tasks are repeatable.  Their history is retained in a
        # separate list when the new generation is initialized.
        if not (q.get('is_repeatable_branch') or q.get('circle_quest_id') or q.get('times_per_day')):
            return False
    prior = q.get('prior_linked_quest')
    if prior and (not linked or not done(character, prior)):
        return False
    if not requirements_met(character, q.get('req', {})):
        return False
    if q.get('aquire_item') and item_count(character, q['aquire_item']) < 1:
        return False
    from quest_controller import repetition_allowed
    if not repetition_allowed(character, number):
        return False
    return sum(active(character, n) for n in character['tasks']) < 20


def preview_allowed(session, npc, quest_no, method):
    q = definition(quest_no)
    if quest_no not in supported() or npc['type'] != 'QuestMan':
        return False
    character = next((c for c in character_store.characters(session)
                      if c['dbid'] == session.selected_character), None)
    if character is None:
        return False
    if character.get('space_no') is not None and npc.get('space_no', character['space_no']) != character['space_no']:
        return False
    progress = character['tasks'].get(str(quest_no), {})
    if method == 'showCompleteQuest':
        return npc['template_id'] in q.get('complete_npcs', []) and progress.get('status') in ('active', 'ready')
    return (npc['template_id'] in q.get('aquire_npcs', []) and can_accept(character, quest_no))


# Compatibility for existing tutorial fixtures; actual routing uses definitions.
INTRO_QUESTS = {n: {'acquire_npc': (definition(n).get('aquire_npcs') or [None])[0],
                  'complete_npc': (definition(n).get('complete_npcs') or [None])[0],
                  'previous': (definition(n).get('req', {}).get('done_quest_and') or
                               [definition(n).get('prior_linked_quest')])[0]}
                for n in range(45195, 45222)}
