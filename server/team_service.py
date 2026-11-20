"""Native five-person offline teams and a recovered starter formation."""
import copy
import functools
import json
import re
import struct

import character_store
from csxy_protocol import ROOT
from entity_methods import avatar_client_message, avatar_property_message
from attribute_service import combat_attributes, notice


@functools.lru_cache(maxsize=1)
def catalog():
    return json.loads((ROOT / 'server/data/team_formations.json').read_text('utf-8'))


def state(character):
    from companion_service import collection
    owned = collection(character)['owned']
    data = copy.deepcopy(character['world_state'].get('offline_team', {}))
    # A free starter formation is an explicit single-player rule, not a grant
    # of unresearched original training levels or formation spells.
    data.setdefault('array', 0)
    keys = ['player'] + list(owned)
    saved = data.get('positions', {})
    positions = {k: saved[k] for k in keys if type(saved.get(k)) is int and 1 <= saved[k] <= 5}
    if len(set(positions.values())) != len(positions):
        raise ValueError('阵法位置重复。')
    for key in keys:
        if key not in positions:
            positions[key] = next(p for p in range(1, 6) if p not in positions.values())
    data['positions'] = positions
    if data['array'] not in (0, 1):
        raise ValueError('该阵法尚未恢复。')
    data.setdefault('allow_join', True)
    return data


def roster(session, character):
    from companion_service import collection, actor_for_member, ENTITY_ID
    rows = [('player', session.entity_id, character['dbid'], character)]
    for key, row in collection(character)['owned'].items():
        eid = ENTITY_ID + row['slot'] - 1
        rows.append((key, eid, -eid, actor_for_member(character, row)))
    return rows


def property_value(session, character):
    from companion_service import collection
    size = 1 + len(collection(character)['owned'])
    return {'leaderID': session.entity_id if size > 1 else 0, 'hugID': 0,
            'size': size, 'allowJoin': state(character)['allow_join'] and size < 5, 'pos': 1}


def messages(session, character, previous=None):
    data = state(character)
    rows = roster(session, character)
    replies = []
    if previous is None or len(rows) == 1:
        replies.append(avatar_client_message(session, 'dismiss_team'))
    else:
        current_ids = {r[1] for r in rows}
        replies.extend(avatar_client_message(session, 'remove_team_member', eid)
                       for _, eid, _, _ in roster(session, previous) if eid not in current_ids)
    replies.extend([avatar_property_message(session, 'team', property_value(session, character)),
                    avatar_client_message(session, 'update_battle_array', [1])])
    if len(rows) > 1:
        for index, (key, eid, dbid, actor) in enumerate(rows):
            school, sex = bytes.fromhex(actor['physique'])[:2]
            replies.append(avatar_client_message(session, 'add_team_member', eid, dbid,
                bytes.fromhex(actor['name']), sex, actor['level'], school,
                index, index + 1, data['positions'][key], 0))
            attrs = combat_attributes(actor)
            replies.extend(avatar_client_message(session, 'update_teammember_combatattr', eid, index, attrs['pro' + str(index)])
                           for index in (7, 8, 100, 101))
            replies.append(avatar_client_message(session, 'update_teammember_location', eid,
                character.get('space_no', getattr(session, 'space_no', 14)),
                character.get('position', session.position)))
    replies.extend([avatar_client_message(session, 'set_battle_array_id', data['array'] if len(rows) > 1 else 0),
                    avatar_client_message(session, 'set_battle_array_famen', 0),
                    avatar_client_message(session, 'team_forbid_join', data.get('forbid_join', False) or len(rows) >= 5)])
    return replies


def request(session, name, arguments):
    formats = {'setBattleArrayID': 'b', 'setBattleArrayPos': 'Ib',
               'setBattleArrayFamen': 'I', 'kickTeamMember': 'I', 'dismissTeam': '',
               'applyQuitTeam': '', 'applyLeaveTeam': '', 'setTeamLeader': 'I',
               'setAllowJoinTeam': 'B', 'setTeamForbidJoin': 'B', 'joinTeam': '', 'remoteJoinTeam': ''}
    if name not in formats:
        return None
    fmt = '<' + formats[name]
    if len(arguments) != struct.calcsize(fmt):
        raise ValueError('invalid team request: ' + name)
    values = struct.unpack(fmt, arguments)
    from companion_service import ready, collection
    from battle_service import selected
    current = selected(session)
    if not current or not ready(session, current):
        return [notice(session, '请在加载和战斗结束后调整队伍。')]
    replies = []
    scene_plan = []

    def change(character):
        if not ready(session, character):
            return
        rows = roster(session, character)
        previous = copy.deepcopy(character)
        owned = collection(character)
        data = state(character)
        if name == 'kickTeamMember':
            key = next((k for k, eid, _, _ in rows if eid == values[0] and k != 'player'), None)
            if not key:
                raise ValueError('请选择已招募的伙伴。')
            del owned['owned'][key]
        elif name in ('dismissTeam', 'applyQuitTeam'):
            owned['owned'].clear()
        elif name == 'applyLeaveTeam':
            raise ValueError('伙伴全部随队参战；请通过解除招募离队。')
        elif name == 'setTeamLeader':
            if values[0] != session.entity_id:
                raise ValueError('单机队伍由玩家担任队长。')
        elif name == 'setBattleArrayID':
            if len(rows) < 2:
                raise ValueError('请先招募伙伴组成队伍。')
            if values[0] not in (0, 1):
                raise ValueError('当前支持无阵和基础花舞阵。')
            data['array'] = values[0]
        elif name == 'setBattleArrayPos':
            key = next((k for k, eid, _, _ in rows if eid == values[0]), None)
            if not key or not 1 <= values[1] <= 5:
                raise ValueError('请选择队伍成员和1至5号阵位。')
            old_position = data['positions'][key]
            for other, position in data['positions'].items():
                if position == values[1]:
                    data['positions'][other] = old_position
                    break
            data['positions'][key] = values[1]
        elif name == 'setBattleArrayFamen':
            if values[0]:
                raise ValueError('阵法法门技能尚未恢复。')
        elif name == 'setAllowJoinTeam':
            if values[0] not in (0, 1):
                raise ValueError('invalid team permission')
            data['allow_join'] = bool(values[0])
        elif name == 'setTeamForbidJoin':
            if values[0] not in (0, 1):
                raise ValueError('invalid team permission')
            data['forbid_join'] = bool(values[0])
        owned['active'] = next(iter(owned['owned']), None)
        character['world_state']['companions'] = owned
        if not owned['owned']:
            data['array'] = 0
        character['world_state']['offline_team'] = data
        character['world_state']['offline_team'] = state(character)
        replies.extend(messages(session, character, previous))
        from companion_scene import prepare
        scene_plan.append(prepare(session, character))

    try:
        result = character_store.transact(session, change)
    except ValueError as error:
        return [notice(session, str(error))]
    if result and scene_plan:
        from companion_scene import apply
        replies.extend(apply(session, scene_plan[0]))
    return replies if result else []


def battle_plan(character, party):
    data = state(character)
    identifier = data['array'] if party else 0
    # Solo encounters always use the original central no-formation position.
    positions = data['positions'] if party else {'player': 1}
    array = catalog()['arrays'][str(identifier)]
    result = {}
    actors = [('player', combat_attributes(character))] + [(c['key'], c['attrs']) for c in party]
    names = {'物攻': 'pro31', '法攻': 'pro41', '物防': 'pro32', '法防': 'pro42',
             '最大生命': 'pro7', '最大法力': 'pro8', '速度': 'pro6'}
    for key, attrs in actors:
        position = array['pos'][str(positions[key])]
        changes = {}
        for buff, _ in position.get('state', []):
            text = catalog()['position_effects'][str(buff)]
            for label, amount in re.findall(r'(物攻|法攻|物防|法防|最大生命|最大法力|速度)\+(\d+)%', text):
                prop = names[label]
                changes[prop] = int(attrs.get(prop, 0) * int(amount) / 100)
        result[key] = {'x': position['x'], 'y': position['y'], 'changes': changes}
    for comp in party:
        row = result[comp['key']]
        comp.update(x=row['x'], y=row['y'])
        comp['attrs'].update({k: comp['attrs'].get(k, 0) + v for k, v in row['changes'].items()})
        for key, targets in {'pro31': ('pro37', 'pro38'), 'pro41': ('pro47', 'pro48')}.items():
            for target in targets:
                comp['attrs'][target] = comp['attrs'].get(target, 0) + row['changes'].get(key, 0)
        comp['attrs']['pro100'] = comp['attrs']['pro7']
        comp['attrs']['pro101'] = comp['attrs']['pro8']
    return identifier, result['player']
