"""Native NPC dialogue bridge for recovered offline quest actions.

This bridge exposes missing task actors/events without changing client
tables. Every dialogue token is short-lived, one-shot and generation-bound.
"""
import copy
import time
import uuid

import character_store
from quest_catalog import catalog, definition, active, can_accept, supported, requirements_met
from quest_controller import auxiliary, item_requirements, apply_action, named_match, matches_category
from entity_methods import avatar_client_message
from attribute_service import notice

PAGE_SIZE = 3  # Three task rows plus one native horizontal navigation row.
NAV_PREVIOUS, NAV_NEXT, NAV_RETURN = 240, 241, 242
# Chang'an's team-service platform: original nearby actors share y=32.25
# (TeamContractor -451.92/450.92, DieRecoverMan -437.38/448.31).
GUIDE_ANCHORS = {11: [-450.0, 32.25, 456.0], 511: [31.0, 62.41, 7.46]}


def guide(session):
    from npc_service import npc_by_template
    space = getattr(session, 'space_no', None)
    if space is None:
        c = current(session)
        space = (c or {}).get('space_no', 14)
    original = npc_by_template(1, kind='QuestMan')
    if not original:
        original = npc_by_template(1, kind='QuestRetrieveMan')
    npc = copy.deepcopy(original)
    npc.update(type=original['type'], template_id=1, entity_id=900000 + int(space or 0),
               space_no=space, position=list(GUIDE_ANCHORS.get(space,
                   [session.position[0] + 3, session.position[1], session.position[2] + 3])),
               name_gbk='任务向导'.encode('gbk').hex(), title_gbk='西游游历'.encode('gbk').hex(), quest_guide=True)
    return npc


def interest(session):
    old = getattr(session, 'quest_guide', None)
    # It remains at the first loaded position until a scene change. Rebuilding
    # it on every movement would place a second model beside the player.
    if not old or old['space_no'] != getattr(session, 'space_no', old['space_no']):
        session.quest_guide = guide(session)
    return session.quest_guide


def current(session):
    return next((c for c in character_store.characters(session) if c['dbid'] == session.selected_character), None)


def dialogue(session, npc, title, options, option_ids=None):
    from npc_service import npc_client_message
    token = uuid.uuid4().bytes
    option_ids = list(range(len(options))) if option_ids is None else list(option_ids)
    if (len(options) > 250 or len(option_ids) != len(options)
            or len(set(option_ids)) != len(option_ids)
            or any(type(i) is not int or not 0 <= i < 255 for i in option_ids)):
        raise ValueError('invalid native dialogue option IDs')
    session.npc_dialog = {'entity_id': npc['entity_id'], 'token': token.hex(),
                          'action': 'quest_offline', 'options': [action for _, action in options],
                          'option_ids': option_ids,
                          'expires': time.time() + 120}
    labels = [(i, label.encode('gbk', 'replace')) for i, (label, _) in zip(option_ids, options)]
    return [npc_client_message(npc, 'dialog_on_cell', token, title, labels, 0)]


def quest_label(number):
    name = definition(number)['name']
    suffix = '（%d）' % number
    limit = max(1, 16 - len(suffix))
    return (name if len(name) <= limit else name[:limit - 1] + '…') + suffix


def open_menu(session, npc, section='main', page=0):
    c = current(session)
    if not c:
        return []
    if section == 'main':
        more = ('副本路线与进度', {'op': 'dungeon_menu'}) if c.get('space_no') == 511 else (
            '更多服务', {'op': 'menu', 'section': 'more'})
        return dialogue(session, npc, '沿途的任务都可以在这里查看。', [
            ('继续当前任务', {'op': 'menu', 'section': 'active'}),
            ('查看可接任务', {'op': 'menu', 'section': 'available'}),
            ('伙伴招募与管理', {'op': 'companion_menu'}),
            more])
    if section == 'more':
        return dialogue(session, npc, '请选择需要的服务。', [
            ('查看循环任务', {'op': 'menu', 'section': 'circle'}),
            ('查询任务编号', {'op': 'search'}),
            ('返回', {'op': 'menu', 'section': 'main'}),
            ('告辞', {'op': 'close'})])
    if section == 'active':
        rows = [int(n) for n in c['tasks'] if int(n) in supported() and active(c, int(n))]
    elif section == 'circle':
        rows = [int(n) for n, q in catalog()['quests'].items()
                if q.get('circle_quest_id') and q.get('is_ring_begin') and not active(c, int(n))
                and int(n) in supported() and can_accept(c, int(n))]
    else:
        rows = [int(n) for n, q in catalog()['quests'].items()
                if not q.get('circle_quest_id') and not q.get('prior_linked_quest')
                and int(n) in supported() and not active(c, int(n)) and can_accept(c, int(n))]
    rows.sort(key=lambda n: (definition(n).get('space_no') != c['space_no'], n))
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    heading = '共%d个 · 第%d/%d页' % (len(rows), page + 1, pages)
    start = page * PAGE_SIZE
    # Preserve IDs to distinguish similarly named tasks; details keep full names.
    options = [(quest_label(n), {'op': 'quest', 'number': n})
               for n in rows[start:start + PAGE_SIZE]]
    option_ids = list(range(len(options)))
    if page:
        options.append(('上一页', {'op': 'menu', 'section': section, 'page': page - 1}))
        option_ids.append(NAV_PREVIOUS)
    if page + 1 < pages:
        options.append(('下一页', {'op': 'menu', 'section': section, 'page': page + 1}))
        option_ids.append(NAV_NEXT)
    options.append(('返回', {'op': 'menu', 'section': 'main'}))
    option_ids.append(NAV_RETURN)
    return dialogue(session, npc, heading if rows else '暂无符合条件的任务。', options, option_ids)


def actions(c, number):
    """Enumerate remaining objectives; no hidden blanket completion action."""
    from quest_catalog import item_count
    q, p = definition(number), c['tasks'][str(number)]
    req, result = q.get('cq', {}).get('req', {}), []
    bind = {'number': number, 'generation': p.get('generation')}
    if number == 14025:
        counts = p.get('monsters', {})
        return [('三大仙真身（%d/3）' % sum(bool(counts.get(str(i), 0)) for i in (33157, 33158, 33159)),
                 dict(bind, op='dungeon_hint'))]
    if q.get('custom_battle') and not p.get('battle_won'):
        result.append(('接受战斗考验', dict(bind, op='battle', custom=q['custom_battle'])))
    if (q.get('quiz1') or q.get('quiz2')) and not p.get('quiz', {}).get('correct'):
        result.append(('回答问题', dict(bind, op='quiz')))
    if (q.get('timer_escort') or q.get('escort_man')) and not p.get('escort_completed'):
        result.append(('开始护送', dict(bind, op='escort')))
    for identifier, limit in req.get('counters', []):
        if p['data'].get('5', {}).get(str(identifier), 0) < limit:
            name = catalog()['counters'].get(str(identifier), {}).get('name', '游历行动')
            combat = any(w in name for w in ('击败', '战胜', '打败', '消灭', '挑战', '击杀'))
            result.append((name, dict(bind, op='battle' if combat else 'action', objective={'kind': 'counter', 'id': identifier})))
    from inventory_service import intro_reward
    for identifier, limit in item_requirements(c, number):
        if item_count(c, identifier) < limit:
            template = intro_reward()['items'][str(identifier)]
            result.append(('寻找%s（%d/%d）' % (template['name'], item_count(c, identifier), limit),
                           dict(bind, op='collect', objective={'kind': 'item', 'id': identifier})))
    rule = req.get('revoke_item_by_name')
    if rule and not any(named_match(i, rule) for i in c['inventory']):
        result.append(('寻找%s' % rule['names'][0], dict(bind, op='collect', objective={'kind': 'named_item'})))
    category = req.get('revoke_item_by_type')
    if category and not any(matches_category(i, category) for i in c['inventory']):
        result.append(('寻找所需材料', dict(bind, op='collect', objective={'kind': 'typed_item'})))
    for field in ('monsters', 'outside_monsters'):
        for identifier, limit in req.get(field, []):
            count = p.get(field, {}).get(str(identifier), 0)
            if count < limit:
                table = catalog()['monsters' if field == 'monsters' else 'born']
                name = table.get(str(identifier), {}).get('name', '任务目标')
                result.append(('击败%s（%d/%d）' % (name, count, limit),
                               dict(bind, op='battle', objective={'kind': field, 'id': identifier})))
    states = c['world_state'].get('quest_states', {})
    for row in req.get('stats', []):
        identifier, limit = (row[:2] if isinstance(row, list) else (row, 1))
        if states.get(str(identifier), 0) < limit:
            result.append(('进行任务行动', dict(bind, op='action', objective={'kind': 'state', 'id': identifier})))
    for identifier in req.get('achieve', []):
        if identifier not in c['world_state'].get('achievements', []):
            result.append(('完成成就考验', dict(bind, op='battle', objective={'kind': 'achievement', 'id': identifier})))
    if q.get('complete_item') and not p.get('item_used'):
        result.append(('请在指定地点使用任务物品', {'op': 'close'}))
    return result


def quest_menu(session, npc, number):
    q, c = definition(number), current(session)
    if not q or number not in supported() or not c:
        return [notice(session, '这个任务已经废弃。')]
    options = []
    p = c['tasks'].get(str(number), {})
    if active(c, number):
        from quest_service import objectives_met
        if objectives_met(c, number):
            options.append(('提交任务', {'op': 'complete', 'number': number, 'generation': p.get('generation')}))
        else:
            options.extend(actions(c, number))
        if q.get('cq', {}).get('req', {}).get('time') and time.time() > p.get('accepted_at', 0) + q['cq']['req']['time']:
            options = [('重新开始限时任务', {'op': 'retry', 'number': number, 'generation': p.get('generation')})]
    elif can_accept(c, number, linked=bool(q.get('prior_linked_quest'))):
        options.append(('接取任务', {'op': 'accept', 'number': number}))
    options.append(('返回', {'op': 'menu', 'section': 'main'}))
    return dialogue(session, npc, q['name'] + '#r' + q.get('desc', '')[:1500], options)


def _valid(session, npc, action):
    from quest_event_service import near
    c = current(session)
    if not near(session, npc, c):
        return False
    number = action.get('number')
    return (number is None or (number in supported() and
            (not action.get('generation') or c['tasks'].get(str(number), {}).get('generation') == action['generation'])))


def answer(session, npc, dialog, option):
    option_ids = dialog.get('option_ids', list(range(len(dialog.get('options', [])))))
    if dialog.get('expires', 0) < time.time() or option not in option_ids:
        return []
    action = dialog['options'][option_ids.index(option)]
    if not _valid(session, npc, action):
        return []
    op, number = action['op'], action.get('number')
    if op == 'close':
        return []
    if op == 'dungeon_menu':
        from dungeon_service import dialogue as dungeon_dialogue
        return dungeon_dialogue(session, npc)
    if op == 'dungeon_hint':
        from dungeon_service import progress_text
        return [notice(session, progress_text(current(session)) +
                       '可找崔大勇进入长生丹室，副本入口向导可查看路线。')]
    if op in ('companion_menu', 'companion'):
        from companion_service import menu, manage
        if op == 'companion_menu':
            return menu(session, npc, action.get('key'), action.get('section'), action.get('page', 0))
        return manage(session, action['command'], action.get('key')) + menu(session, npc, action.get('key'))
    if op == 'menu':
        return open_menu(session, npc, action.get('section', 'main'), action.get('page', 0))
    if op == 'quest':
        return quest_menu(session, npc, number)
    if op == 'search':
        from npc_service import npc_client_message
        token = uuid.uuid4().bytes
        session.npc_dialog = {'entity_id': npc['entity_id'], 'token': token.hex(), 'action': 'quest_search', 'expires': time.time() + 120}
        return [npc_client_message(npc, 'input_on_cell', token, '查询任务', '请输入任务编号', '', '', 1, 8)]
    if op in ('accept', 'complete'):
        from quest_service import accept, complete
        return accept(session, npc, number) if op == 'accept' else complete(session, npc, number)
    if op == 'retry':
        def retry(c):
            if c['tasks'][str(number)].get('generation') != action['generation']:
                return
            from quest_service import initialize
            initialize(c, number)
        c = character_store.transact(session, retry)
        return snapshot(session, c, [number]) + quest_menu(session, npc, number)
    if op == 'quiz':
        return quiz_menu(session, npc, number)
    if op == 'quiz_answer':
        from quest_controller import answer_quiz
        done = []
        def quiz(c):
            done.append(answer_quiz(c, number, action['option']))
        c = character_store.transact(session, quiz)
        return snapshot(session, c, [number]) + [notice(session, '答对了！' if done[0] else '回答不正确，请再试一次。')]
    if op == 'battle':
        from battle_service import start
        target = dict(npc, offline_encounter={'quest': number, 'generation': action['generation'],
                                            'objective': action.get('objective'), 'custom_id': action.get('custom')})
        return start(session, target, story_quest=number)
    if op == 'escort':
        route = escort_route(number, current(session), npc)
        def escort(c):
            c['tasks'][str(number)]['escort_route'] = route
            c['tasks'][str(number)]['escort_node'] = 0
        character_store.transact(session, escort)
        return [notice(session, '护送开始。' + escort_hint(route, 0), chat=True)]
    if op == 'collect':
        def begin(c):
            c['world_state']['offline_quest_action'] = dict(action, due=time.time() + 2,
                space=c['space_no'], position=list(session.position), npc=npc['entity_id'])
        character_store.transact(session, begin)
        return [avatar_client_message(session, 'begin_intonate', 2, '正在寻找…')]
    if op == 'action':
        before = current(session)
        def perform(c):
            apply_action(c, number, action['objective'])
        c = character_store.transact(session, perform)
        return snapshot(session, c, [number], before) + quest_menu(session, npc, number)
    return []


def snapshot(session, c, numbers, before=None):
    from quest_service import client_quest_data, item_objective_messages, counter_messages
    from inventory_service import delta_messages
    packets = delta_messages(session, before, c) if before else []
    packets += counter_messages(session, c, numbers) + item_objective_messages(session, c)
    for number in numbers:
        for key, value in client_quest_data(c).get(number, {}).items():
            if key not in (0, 101):
                packets.append(avatar_client_message(session, 'update_quest_data', number, key, value))
    return packets


def quiz_menu(session, npc, number):
    q, c = definition(number), current(session)
    table = auxiliary()['wen_question' if q.get('quiz2') else 'keju_question']
    identifier = q.get('quiz2') or q.get('quiz1')
    row = table.get(str(identifier)) or table[str(sorted(map(int, table))[identifier % len(table)])]
    def ask(character):
        character['tasks'][str(number)].setdefault('quiz', {})['question_id'] = row['id']
    character_store.transact(session, ask)
    return dialogue(session, npc, row['question'], [(text, {'op': 'quiz_answer', 'number': number,
                     'generation': c['tasks'][str(number)]['generation'], 'option': int(key)})
                    for key, text in row['opt'].items()])


def escort_route(number, character, npc):
    q = definition(number)
    original = auxiliary()['escort_man_data'].get(str(q.get('escort_man')), {})
    if original:
        return [{'space': original['space_no'], 'pos': row['pos']} for _, row in sorted(original['node'].items(), key=lambda r: int(r[0])) if row.get('pos')]
    from npc_service import npc_by_template
    for identifier in q.get('complete_npcs', []):
        actor = npc_by_template(identifier, kind='QuestMan')
        if actor:
            return [{'space': actor['space_no'], 'pos': actor['position']}]
    return [{'space': character['space_no'], 'pos': [npc['position'][0] + 35, npc['position'][1], npc['position'][2]]}]


def escort_hint(route, node):
    if node >= len(route):
        return '护送已到达终点，请交还任务。'
    target = route[node]
    name = catalog()['spaces'].get(str(target['space']), {}).get('name', str(target['space']))
    return '下一站：%s（%.1f，%.1f），第 %d/%d 站。' % (
        name, target['pos'][0], target['pos'][2], node + 1, len(route))


def tick(session, now=None):
    now = time.time() if now is None else now
    if not session.world_ready or getattr(session, 'battle_id', ''):
        return []
    c = current(session)
    action = (c or {}).get('world_state', {}).get('offline_quest_action')
    if not action:
        return []
    if now < action['due']:
        return []
    changed = []
    before = copy.deepcopy(c)
    def finish(ch):
        pending = ch['world_state'].pop('offline_quest_action', None)
        p = ch['tasks'].get(str(action['number']), {})
        if (pending != action or p.get('generation') != action['generation'] or not active(ch, action['number'])
                or ch['space_no'] != action['space']
                or sum((session.position[i] - action['position'][i]) ** 2 for i in (0, 2)) > 4):
            return
        apply_action(ch, action['number'], action['objective'])
        changed.append(action['number'])
    try:
        c = character_store.transact(session, finish)
    except ValueError as error:
        character_store.transact(session, lambda ch: ch['world_state'].pop('offline_quest_action', None))
        return [avatar_client_message(session, 'stop_intonate'), notice(session, str(error))]
    return [avatar_client_message(session, 'stop_intonate')] + snapshot(session, c, changed, before)


def movement(session):
    c, changed, hints = current(session), [], []
    if not c or not session.world_ready or getattr(session, 'battle_id', ''):
        return []
    def advance(ch):
        for key, p in ch['tasks'].items():
            if not active(ch, int(key)):
                continue
            route, node = p.get('escort_route', []), p.get('escort_node', 0)
            if node < len(route) and route[node]['space'] == ch['space_no'] and sum(
                    (session.position[i] - route[node]['pos'][i]) ** 2 for i in (0, 2)) <= 25 ** 2:
                p['escort_node'] = node + 1
                if node + 1 == len(route):
                    apply_action(ch, int(key), {'kind': 'escort'})
                    # A validated route arrival substitutes the missing cell
                    # script, including the escort's original counters.
                    for identifier, amount in definition(int(key)).get('cq', {}).get('req', {}).get('counters', []):
                        p['data'].setdefault('5', {})[str(identifier)] = amount
                    from quest_service import objectives_met
                    p['status'] = 'ready' if objectives_met(ch, int(key)) else 'active'
                hints.append(escort_hint(route, node + 1))
                changed.append(int(key))
    c = character_store.transact(session, advance)
    return (snapshot(session, c, changed) + [notice(session, h, chat=True) for h in hints]) if changed else []


def circle_begin(session, npc, identifier):
    library = auxiliary()['circle_quest_lib'].get(str(identifier), {})
    if not library or library.get('shuted') or npc['template_id'] not in library.get('aquire_npcs', []):
        return []
    c = current(session)
    rows = [n for n in library['quest_list'] if n in supported() and not active(c, n)
            and can_accept(c, n, linked=bool(definition(n).get('prior_linked_quest')))]
    for key, p in c['tasks'].items():
        if active(c, int(key)) and definition(int(key)).get('circle_quest_id') == identifier:
            return [notice(session, '请先完成本轮任务。')]
    if not rows:
        return [notice(session, '尚未满足本轮任务的等级或前置条件。')]
    state = c.get('world_state', {}).get('circle_quests', {}).get(str(identifier), {})
    number = rows[state.get('done_today', 0) % len(rows)]
    # The library giver may differ from the task's destination actor.
    from quest_service import accept
    source = dict(npc, quest_guide=True, circle_accept=identifier)
    return accept(session, source, number)
