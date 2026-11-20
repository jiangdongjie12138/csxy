"""Persisted Changsheng instance and original native doors/quest targets."""
import copy
import functools
import json
import struct
import time
import uuid

import character_store
from csxy_protocol import ROOT, variable_message
from entity_methods import avatar_client_message
from attribute_service import notice

SPACE = 511
STATE = 'changsheng_dungeon'
SWITCH_IDS = {600: 9100600, 601: 9100601}
FORM_IDS = {33361: 9100611, 33362: 9100612, 33363: 9100613}
FINAL_EXIT_ID = 9100606


@functools.lru_cache(maxsize=1)
def catalog():
    return json.loads((ROOT / 'server/data/changsheng_dungeon.json').read_text('utf-8'))


def state(character):
    return (character or {}).get('world_state', {}).get(STATE, {})


def current(session):
    from battle_service import selected
    return selected(session)


def active(character):
    data = state(character)
    return (character and character.get('space_no') == SPACE
            and data.get('id') == SPACE and data.get('run_id')
            and data.get('expires_at', 0) > time.time())


def near(session, npc, character):
    from mount_service import ready
    return (character and ready(session, character)
            and npc['entity_id'] in session.visible_npcs
            and character.get('space_no') == npc['space_no']
            and sum((session.position[i] - npc['position'][i]) ** 2 for i in (0, 2)) <= 25 ** 2)


def transfer(session, npc, destination):
    """Validate and fully encode inside the latest-save transaction."""
    from world_service import prepare_scene_transfer, apply_scene_transfer
    plans, replies = [], []

    def change(character):
        if not near(session, npc, character):
            return
        data = catalog()
        saved = copy.deepcopy(state(character))
        if destination == 'enter':
            if npc['type'] != 'FubenEntryman' or npc['template_id'] != data['entry'] or npc['space_no'] != 11:
                return
            if character['level'] < data['definition']['req']['min_lv']:
                raise ValueError('长生丹室需要达到20级。')
            from team_service import roster
            if len(roster(session, character)) > data['definition']['max_player']:
                raise ValueError('长生丹室队伍最多5人。')
            if (not saved.get('run_id') or saved.get('expires_at', 0) <= time.time()
                    or 704 in saved.get('events', [])):
                saved = {'version': 1, 'id': SPACE, 'run_id': uuid.uuid4().hex,
                         'events': [], 'defeated': [], 'true_forms': [],
                         'started_at': time.time(),
                         'expires_at': time.time() + data['single_player_policy']['lifetime_seconds']}
            saved['inside'] = True
            position, space = data['definition']['in']['pos'], SPACE
        elif destination == 'exit':
            if character['space_no'] != SPACE:
                return
            saved['inside'] = False
            target = data['definition']['out']
            position, space = target['pos'], target['space_no']
            replies.append(avatar_client_message(session, 'exit_fuben'))
        else:
            if not active(character):
                raise ValueError('副本已超时，请离开后重新进入。')
            events = saved.get('events', [])
            routes = {'upper': [-2.53, 32.22, 8.69],
                      'lower': [-0.96, 7.24, -2.9], 'final': [0.8, 6.54, -49.0]}
            if destination not in routes:
                return
            if destination == 'lower' and 702 not in events:
                raise ValueError('请先揭穿杨道长，打开上层机关。')
            if destination == 'final' and 705 not in events:
                raise ValueError('请先揭穿三位道长，打开下层机关。')
            position, space = routes[destination], SPACE
        character['world_state'][STATE] = saved
        character.update(space_no=space, position=list(position))
        plan = prepare_scene_transfer(session, character)
        if plan is None:
            raise ValueError('请等待场景加载或战斗结束。')
        plans.append(plan)

    try:
        result = character_store.transact(session, change)
    except ValueError as error:
        return [notice(session, str(error))]
    if not result or not plans:
        return []
    if destination == 'exit':
        session.dungeon_ui_id = 0
    return replies + apply_scene_transfer(session, plans[0])


def dialogue(session, npc):
    from npc_service import npc_client_message
    character = current(session)
    if not near(session, npc, character):
        return []
    if npc['type'] == 'FubenEntryman':
        options = [('进入长生丹室', 'enter'), ('告辞', 'close')]
        title = '长生丹室 · 20级开放 · 玩家与伙伴最多5人#r中途离开可继续未完成的进度。'
    else:
        events = state(character).get('events', [])
        options = [('前往地宫上层', 'upper')]
        if 702 in events:
            options.append(('前往地宫下层', 'lower'))
        if 705 in events:
            options.append(('前往三大仙所在处', 'final'))
        options.append(('离开长生丹室', 'exit'))
        title = '揭穿道长：%d/3 · 驱逐真身：%d/3' % (
            len(set(events) & {701, 702, 703}), len(state(character).get('true_forms', [])))
    token = uuid.uuid4().bytes
    session.npc_dialog = {'entity_id': npc['entity_id'], 'token': token.hex(),
                          'action': 'dungeon', 'options': [op for _, op in options],
                          'expires': time.time() + 120,
                          'run_id': state(character).get('run_id')}
    return [npc_client_message(npc, 'dialog_on_cell', token, title,
             [(i, label.encode('gbk')) for i, (label, _) in enumerate(options)], 0)]


def answer(session, npc, dialog, selection):
    if (dialog.get('expires', 0) < time.time() or not 0 <= selection < len(dialog['options'])
            or dialog.get('run_id') != state(current(session)).get('run_id')):
        return []
    op = dialog['options'][selection]
    return [] if op == 'close' else transfer(session, npc, op)


def request(session, npc, name, arguments):
    if npc['type'] == 'FubenEntryman' and npc['template_id'] == catalog()['entry'] and npc['space_no'] == 11:
        if name in ('registerFuben', 'enterFuben'):
            if len(arguments) != 4:
                raise ValueError('Fuben request needs UINT32 FUBEN_ID')
            if struct.unpack('<I', arguments)[0] != SPACE:
                return []
            return transfer(session, npc, 'enter')
        if name == 'use':
            return dialogue(session, npc)
    if npc['space_no'] == SPACE:
        if name == 'use' and npc['type'] == 'FubenExitman':
            return transfer(session, npc, 'exit')
        if name == 'use' and npc['type'] == 'DungeonSwitch':
            key = npc['template_id']
            opened = switch_status(current(session), key)
            return [notice(session, '机关已打开。' if opened else
                           ('请先揭穿杨道长。' if key == 600 else '请先揭穿三位道长。'))]
    return None


def switch_status(character, key):
    return int((702 if key == 600 else 705) in state(character).get('events', []))


def scene_npcs(session, character):
    if (character or {}).get('space_no') != SPACE:
        return []
    from npc_service import npc_by_template
    result = []
    for key, eid in SWITCH_IDS.items():
        switch = catalog()['switches'][str(key)]
        result.append({'entity_id': eid, 'type': 'DungeonSwitch', 'template_id': key,
                       'space_no': SPACE, 'position': switch['pos'],
                       'status': switch_status(character, key), 'switch': True})
    if 705 in state(character).get('events', []):
        original = npc_by_template(33361, SPACE, 'Monster')
        from quest_catalog import catalog as quests
        for key, eid in FORM_IDS.items():
            if key == 33361:
                continue  # Its original static spawn is retained.
            born = quests()['born'][str(key)]
            npc = copy.deepcopy(original)
            npc.update(template_id=key, entity_id=eid, aspect=born['aspect'],
                       name_gbk=born['name'].encode('gbk').hex(),
                       position=catalog()['true_forms'][str(key)]['position'])
            result.append(npc)
    if 704 in state(character).get('events', []):
        npc = copy.deepcopy(npc_by_template(catalog()['exit'], SPACE, 'FubenExitman'))
        npc.update(entity_id=FINAL_EXIT_ID,
                   position=catalog()['events']['704']['finish']['fubenexitmen'][0][0])
        result.append(npc)
    return result


def find_npc(session, eid):
    if eid not in {*SWITCH_IDS.values(), *FORM_IDS.values(), FINAL_EXIT_ID}:
        return None
    return next((npc for npc in scene_npcs(session, current(session)) if npc['entity_id'] == eid), None)


def visible(npc, character):
    if npc['space_no'] != SPACE or npc['type'] != 'Monster':
        return True
    saved = state(character)
    if not active(character) or npc['entity_id'] in saved.get('defeated', []):
        return False
    rule = catalog()['stages'].get(str(npc['template_id']))
    if rule:
        if rule['event'] in saved.get('events', []):
            return False
        # Recovered placement lists include multiple copies of each boss.
        # One stable original spawn represents each stage in a solo instance.
        from npc_service import npc_by_template
        original = npc_by_template(npc['template_id'], SPACE, 'Monster')
        if not original or npc['entity_id'] != original['entity_id']:
            return False
        return npc['template_id'] != 33360 or 702 in saved.get('events', [])
    if str(npc['template_id']) in catalog()['true_forms']:
        return 705 in saved.get('events', []) and npc['template_id'] not in saved.get('true_forms', [])
    return True


def create_switch(npc):
    # Runtime Switch indices: spaceNo=0 (own), visible=1, switchID=2, status=3.
    props = b'\x03\x01\x01\x02' + struct.pack('<I', npc['template_id']) + b'\x03' + bytes([npc['status']])
    return variable_message(9, struct.pack('<IH3f3b', npc['entity_id'],
        catalog()['switch_client_type'], *npc['position'], 0, 0,
        127 if npc['template_id'] == 601 else 0) + props)


def switch_messages(session, character):
    old = getattr(session, 'dungeon_switch_states', {})
    wanted, packets = {}, []
    for key, eid in SWITCH_IDS.items():
        if eid not in session.visible_npcs:
            continue
        status = switch_status(character, key)
        wanted[str(eid)] = status
        if str(eid) in old and old[str(eid)] != status:
            packets.append(variable_message(0xc3, struct.pack('<IB', eid, status)))
    session.dungeon_switch_states = wanted
    return packets


def encounter(character, npc):
    if npc['space_no'] != SPACE:
        return None
    if not visible(npc, character) or not active(character):
        raise ValueError('请先完成前面的副本阶段，或重新进入副本。')
    from quest_catalog import catalog as quests, active as quest_active
    data = quests()
    form = catalog()['true_forms'].get(str(npc['template_id']))
    monster = (data['monsters'][str(form['monster'])] if form else
               {'id': npc['template_id'], 'aspect': npc['aspect'],
                'name': bytes.fromhex(npc['name_gbk']).decode('gbk')})
    rule = catalog()['stages'].get(str(npc['template_id']))
    custom = data['battles'].get(str(rule['battle'] if rule else 7540), {})
    number = catalog()['quest'] if quest_active(character, catalog()['quest']) else 0
    return {'quest': number, 'generation': character['tasks'].get(str(number), {}).get('generation'),
            'monster': monster, 'born_id': npc['template_id'], 'level': max(20, min(30, character['level'])),
            'drops': [], 'field': 'universes/eg/wf/' + custom.get('map', 'wf_hend06'),
            'story': True, 'dungeon_run': state(character)['run_id']}


def victory(character, battle):
    encounter = battle.get('encounter', {})
    if not encounter.get('dungeon_run'):
        return
    saved = state(character)
    if saved.get('run_id') != encounter['dungeon_run'] or not active(character):
        raise ValueError('副本进度已经变化或超时，请退出战斗。')
    number = encounter.get('quest', 0)
    if number and character['tasks'].get(str(number), {}).get('generation') != encounter.get('generation'):
        raise ValueError('任务已重新接取，请重新开始战斗。')
    if battle['npc'] in saved['defeated']:
        raise ValueError('该副本目标已经击败。')
    saved['defeated'].append(battle['npc'])
    before = set(saved['events'])
    rule = catalog()['stages'].get(str(encounter['born_id']))
    if rule:
        saved['events'].append(rule['event'])
    if {701, 702, 703} <= set(saved['events']) and 705 not in saved['events']:
        saved['events'].append(705)
    if str(encounter['born_id']) in catalog()['true_forms']:
        saved['true_forms'].append(encounter['born_id'])
        if len(set(saved['true_forms'])) == 3 and 704 not in saved['events']:
            saved['events'].append(704)
    battle['dungeon_events'] = sorted(set(saved['events']) - before)


def messages(session, character, loaded=False):
    if (character or {}).get('space_no') != SPACE:
        if getattr(session, 'dungeon_ui_id', 0):
            session.dungeon_ui_id = 0
            return [avatar_client_message(session, 'exit_fuben')]
        return []
    saved = state(character)
    packets = []
    if loaded:
        session.dungeon_ui_id = SPACE
        packets.append(avatar_client_message(session, 'enter_fuben', SPACE,
            max(0, int(saved.get('expires_at', 0) - time.time()))))
    packets.append(avatar_client_message(session, 'event_fuben', saved.get('events', [])))
    return packets


def progress_text(character):
    """Distinguish revealing the priests from the task's true-form kills."""
    saved = state(character)
    events = set(saved.get('events', []))
    priests = ((701, '陆道长'), (702, '杨道长'), (703, '胡道长'))
    count = len(events & {701, 702, 703})
    forms = len(set(saved.get('true_forms', [])))
    text = '揭穿道长：%d/3；击败三大仙真身：%d/3。' % (count, forms)
    if count < 3:
        remaining = '、'.join(name for event, name in priests if event not in events)
        return text + '下一步揭穿%s。三大仙任务的击杀记录在击败真身后增加。' % remaining
    if forms < 3:
        names = ((33361, '鹿力'), (33362, '羊力'), (33363, '虎力'))
        remaining = '、'.join(name for key, name in names if key not in saved.get('true_forms', []))
        return text + '前往下层深处，击败%s真身。' % remaining
    return text + '返回崔府找崔大忠交任务。'
