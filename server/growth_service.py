"""Original point allocation and level formulas with explicit simulator rates."""
import functools
import json
import logging

import character_store
from csxy_protocol import ROOT

LOG = logging.getLogger('growth')


@functools.lru_cache(maxsize=1)
def rules():
    return json.loads((ROOT / 'server/data/attribute_growth.json').read_text('utf-8'))


def allocated(character):
    return {key: int(character['combat_attr'].get(key, 0)) for key in rules()['allocation_order']}


def potential(character):
    # Entitlement is derived from saved level and allocations, so legacy levels
    # are repaired automatically and relogin cannot grant the same points again.
    return max(0, (character['level'] - 1) * rules()['free_points_per_level']
               - sum(allocated(character).values()))


def coefficients(character):
    """Share the same restored/fallback rates for level, points and equipment."""
    values = dict(rules()['simulator_coefficients'])
    # A subsequently recovered per-character rate can replace a fallback.
    for key in values:
        if key in character['combat_attr']:
            values[key] = character['combat_attr'][key]
    return values


def contributions(character):
    """Read-only level contribution; never store equipment/derived totals."""
    level = character['level']
    extra_levels = level - 1
    points = allocated(character)
    rates = coefficients(character)
    values = {key: level + points[manual]
              for key, manual in rules()['primary_attributes'].items()}
    values.update(points)
    for target, grow, point, factor in (
            ('pro7', 'hp_grow', 'pro111', 'gengu_xishu'),
            ('pro8', 'mp_grow', 'pro113', 'lingli_xishu'),
            ('pro6', 'sudu_grow', 'pro112', 'minjie_xishu'),
            ('pro31', 'wl_gongji_grow', 'pro110', 'liliang_xishu'),
            ('pro41', 'fs_gongji_grow', 'pro114', 'zhihui_xishu')):
        values[target] = (extra_levels * (rates[grow] + rates[factor])
                          + points[point] * rates[factor])
    values['pro32'] = extra_levels * rates['wl_fangyu_grow']
    values['pro42'] = extra_levels * rates['fs_fangyu_grow']
    return values


def reconcile(character, *, refill=False):
    """Repair capacity once; actual level rewards also refill both vitals."""
    from attribute_service import combat_attributes
    saved = character['world_state'].get('attribute_growth', {})
    if not refill and not saved and character['level'] == 1:
        return False  # No growth to reconcile; initial login stays read-only.
    previous_level = saved.get('applied_level', 1)
    if not refill and previous_level >= character['level'] and saved.get('policy') == rules()['policy']:
        return False
    before = combat_attributes(dict(character, level=previous_level))
    after = combat_attributes(character)
    for current, maximum in (('pro100', 'pro7'), ('pro101', 'pro8')):
        previous = before[current]
        value = (after[maximum] if refill else
                 min(after[maximum], previous + max(0, after[maximum] - before[maximum])))
        if not refill and current == 'pro100' and previous <= 0:
            value = 0
        character['combat_attr'][current] = int(value)
    character['world_state']['attribute_growth'] = {
        'policy': rules()['policy'], 'applied_level': character['level']}
    return True


def repair(session):
    repaired = []

    def change(character):
        if reconcile(character):
            repaired.append(True)

    character = character_store.transact(session, change)
    if repaired and character is not None:
        LOG.info('Level growth reconciled and saved: level=%d potential=%d',
                 character['level'], potential(character))
    return character


def allocate(session, amounts):
    from attribute_service import message, notice
    from entity_methods import avatar_client_message
    if not session.world_ready or getattr(session, 'battle_id', ''):
        return []
    if (len(amounts) != 5 or any(type(amount) is not int or amount < 0 for amount in amounts)
            or not sum(amounts)):
        return [notice(session, '加点数量无效。')]

    def change(character):
        if sum(amounts) > potential(character):
            raise ValueError('剩余潜力点不足。')
        points = allocated(character)
        for key, amount in zip(rules()['allocation_order'], amounts):
            if points[key] + amount > 999:
                raise ValueError('单项加点超过上限。')
        reconcile(character)
        for key, amount in zip(rules()['allocation_order'], amounts):
            character['combat_attr'][key] = points[key] + amount

    try:
        character = character_store.transact(session, change)
    except ValueError as error:
        return [notice(session, str(error))]
    if character is None:
        return []
    LOG.info('Potential allocated and saved: used=%d remaining=%d', sum(amounts), potential(character))
    return [message(session, character), avatar_client_message(session, 'update_potential', potential(character))]
