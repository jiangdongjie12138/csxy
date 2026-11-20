"""Persistent participant XP and native manual leveling with original thresholds."""
import functools
import json
import logging

import character_store
from attribute_service import notice
from csxy_protocol import ROOT
from entity_methods import avatar_client_message
from quest_catalog import catalog
from summon_service import PAGE, usable

LOG = logging.getLogger('summon-progress')


@functools.lru_cache(maxsize=1)
def rules():
    return json.loads((ROOT / 'server/data/summon_progression.json').read_text('utf-8'))


def threshold(level):
    row = catalog()['experience'].get(str(level))
    return row['summon'] if row else rules()['maximum_experience'] + 1


def participant(character, battle):
    return next((i for i in character['inventory'] if i['page'] == PAGE
                 and i['uuid'] == battle.get('summon_uuid')
                 and str(i['id']) in catalog()['summons']), None)


def grant_battle_experience(character, battle):
    # This runs inside the victory transaction. The recorded UUID binds the
    # award to the actual participant rather than the currently selected pet.
    if 'summon_experience' in battle:
        return battle['summon_experience']
    reward = {}
    item = participant(character, battle) if battle.get('summon_joined') else None
    if item and battle.get('outcome') == 'victory':
        props = item['attributes']
        level = props['combatAttr']['pro106']
        if (level < rules()['maximum_level'] and props.get('slife', 0) > 0
                and props['combatAttr'].get('pro100', 0) > 0):
            enemy_level = max(1, min(130, int(battle.get('encounter', {}).get('level', 1))))
            offered = max(1, int(threshold(enemy_level)
                                * rules()['simulator_rules']['battle_experience_threshold_fraction']))
            current = props['exp']
            ceiling = min(rules()['maximum_experience'],
                          threshold(level) * rules()['maximum_threshold_multiple'])
            amount = max(0, min(offered, ceiling - current))
            if amount:
                props['exp'] += amount
                reward = {'uuid': item['uuid'], 'experience': amount,
                          'policy': 'simulator-summon-xp-v1'}
    battle['summon_experience'] = reward
    return reward


def experience_messages(session, character, battle):
    reward = battle.get('summon_experience', {})
    item = participant(character, battle)
    if not reward or not item or item['uuid'] != reward['uuid']:
        return []
    # res_setattr updates the existing instance and Package's signals. No
    # remove/insert or duplicate item is needed to refresh the native XP bar.
    return [avatar_client_message(session, 'res_setattr', 0, PAGE, item['pos'],
                                  [('exp', item['attributes']['exp'])])]


def level_attributes(item, *, refill=True):
    """Recovered base level/primary formulas, preserving the saved growth rates.

    Advanced spell/state/inner-core effects remain outside this basic server
    calculator. The native client still applies its original passive effects.
    """
    data = catalog()['summons'][str(item['id'])]
    attrs = item['attributes']['combatAttr']
    level = attrs['pro106']
    base = {key: value + level * data.get('grow', {}).get(key, 0)
            for key, value in data.get('attrs', {}).items()}
    for target, manual in (('pro1', 'pro111'), ('pro2', 'pro113'), ('pro3', 'pro110'),
                           ('pro4', 'pro112'), ('pro5', 'pro114')):
        attrs[target] = level + attrs.get(manual, 0)
    for target, grow, primary, coefficient in (
            ('pro7', 'hp_grow', 'pro1', 'gengu_xishu'),
            ('pro8', 'mp_grow', 'pro2', 'lingli_xishu'),
            ('pro6', 'sudu_grow', 'pro4', 'minjie_xishu'),
            ('pro31', 'wl_gongji_grow', 'pro3', 'liliang_xishu'),
            ('pro41', 'fs_gongji_grow', 'pro5', 'zhihui_xishu')):
        value = (base.get(target[3:], 0) + level * attrs.get(grow, 0)
                 + attrs[primary] * data.get('xishu', {}).get(coefficient, 0))
        attrs[target] = int(value) if target in ('pro7', 'pro8') else value
    for target, grow in (('pro32', 'wl_fangyu_grow'), ('pro42', 'fs_fangyu_grow')):
        attrs[target] = base.get(target[3:], 0) + level * attrs.get(grow, 0)
    for target, attack, weapon, coefficient in (
            ('pro37', 'pro31', 'pro121', 'pro39'), ('pro38', 'pro31', 'pro121', 'pro40'),
            ('pro47', 'pro41', 'pro122', 'pro49'), ('pro48', 'pro41', 'pro122', 'pro50')):
        attrs[target] = attrs[attack] + attrs.get(weapon, 0) * attrs.get(coefficient, 0)
    # Original summonLevelUp -> summonFull restores HP, MP and loyalty.
    if refill:
        attrs['pro100'], attrs['pro101'] = attrs['pro7'], attrs['pro8']


def allocate(session, pos, amounts):
    if (getattr(session, 'superseded', False) or not session.world_ready
            or getattr(session, 'battle_id', '') or getattr(session, 'scene_transfer', {})
            or getattr(session, 'scene_messages', [])):
        return []
    if len(amounts) != 5 or any(type(n) is not int or not 0 <= n <= 65535 for n in amounts):
        return [notice(session, '召唤兽加点参数无效。')]
    if not sum(amounts):
        return []
    replies = []

    def change(character):
        if character.get('world_state', {}).get('tutorial_battle', {}).get('phase') in (
                'preparing', 'input', 'animating'):
            return
        item = next((i for i in character['inventory'] if i['page'] == PAGE and i['pos'] == pos), None)
        if item is None or not usable(character, item):
            raise ValueError('请选择可用的召唤兽。')
        props = item['attributes']
        if sum(amounts) > props.get('potential', 0):
            raise ValueError('召唤兽潜力点不足。')
        attrs = props['combatAttr']
        # Original wire/UI order: COR, SPI, AGI, STR, WIS.
        for key, amount in zip(('pro111', 'pro113', 'pro112', 'pro110', 'pro114'), amounts):
            attrs[key] = attrs.get(key, 0) + amount
        props['potential'] -= sum(amounts)
        hp, mp = attrs['pro100'], attrs['pro101']
        level_attributes(item, refill=False)
        rates = catalog()['summons'][str(item['id'])].get('xishu', {})
        # summonUsePotential adds only the capacity gained through COR/SPI,
        # then summonPadHpMp clamps; allocation never performs summonFull.
        attrs['pro100'] = int(min(attrs['pro7'], max(0, hp + amounts[0] * rates.get('gengu_xishu', 0))))
        attrs['pro101'] = int(min(attrs['pro8'], max(0, mp + amounts[1] * rates.get('lingli_xishu', 0))))
        replies.append(avatar_client_message(session, 'res_setattr', 0, PAGE, pos,
                                             [('combatAttr', attrs), ('potential', props['potential'])]))

    try:
        character = character_store.transact(session, change)
    except ValueError as error:
        return [notice(session, str(error))]
    if character is None or not replies:
        return []
    LOG.info('Summon potential committed: slot=%d used=%d', pos, sum(amounts))
    return replies


def level_up(session, pos):
    if (getattr(session, 'superseded', False) or not session.world_ready
            or getattr(session, 'battle_id', '') or getattr(session, 'scene_transfer', {})
            or getattr(session, 'scene_messages', [])):
        return []
    replies = []

    def change(character):
        if character.get('world_state', {}).get('tutorial_battle', {}).get('phase') in (
                'preparing', 'input', 'animating'):
            return
        item = next((i for i in character['inventory'] if i['page'] == PAGE and i['pos'] == pos), None)
        if item is None or not usable(character, item):
            raise ValueError('请选择可用的召唤兽。')
        props = item['attributes']
        level = props['combatAttr']['pro106']
        if level >= min(rules()['maximum_level'], character['level'] + rules()['maximum_above_owner']):
            raise ValueError('召唤兽已达到当前等级上限。')
        need = threshold(level)
        if props['exp'] < need:
            raise ValueError('召唤兽经验不足，升级需要%d经验。' % need)
        props['exp'] -= need
        props['combatAttr']['pro106'] += 1
        props['potential'] += rules()['points_per_level']
        props['loy'] = rules()['maximum_loyalty']
        level_attributes(item)
        updates = [(name, props[name]) for name in ('exp', 'combatAttr', 'potential', 'loy')]
        replies.append(avatar_client_message(session, 'res_setattr', 0, PAGE, pos, updates))
        replies.append(notice(session, '%s升到了%d级，获得%d潜力点。' % (
            props['sname'], level + 1, rules()['points_per_level']), chat=True))

    try:
        character = character_store.transact(session, change)
    except ValueError as error:
        return [notice(session, str(error))]
    if character is None or not replies:
        return []
    LOG.info('Summon level committed: slot=%d', pos)
    return replies
