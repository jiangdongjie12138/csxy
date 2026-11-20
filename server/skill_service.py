"""Original role skill learning/consumption with bounded simulator effects."""
import functools
import json
import logging
import math

import character_store
from csxy_protocol import ROOT
from entity_methods import avatar_client_message

LOG = logging.getLogger('skills')
STATE_KEY = 'role_skills'


@functools.lru_cache(maxsize=1)
def catalog():
    return json.loads((ROOT / 'server/data/character_skills.json').read_text('utf-8'))


def rank(identifier, level):
    return catalog()['ranks'].get(str(identifier), {}).get(str(level))


def learned(character):
    return character['world_state'].get(STATE_KEY, {})


def supported(row):
    if not row:
        return False
    spell = row['spell']
    if (spell.get('target_type') not in (1, 4, 5) or spell.get('type') not in (1, 2)
            or spell.get('delay_round') or spell.get('speed_consume')
            or spell.get('consume', {}).get('ba_point') or spell.get('control')):
        return False
    effects = spell.get('effect', {})
    if any(e.get('attr') != 'pro100' or e.get('formula') not in (11, 12, 15)
           for e in effects.values()):
        return False
    states = [s for key in ('buff', 'target_buff') for s in spell.get(key, {}).values()]
    if not effects and not states:
        return False
    for state in states:
        buff = catalog()['buffs'].get('%d:%d' % (state['id'], state['level']))
        if buff is None or buff.get('type') == 10:
            return False  # Native form/model changes have not been restored.
        for effect in buff.get('effect', {}).values():
            if effect.get('formula') == 1 and effect.get('attr') in ('av31', 'av32', 'av41', 'av42'):
                continue
            if effect.get('attr') == 'av100' and effect.get('formula') in (11, 12, 16):
                continue
            return False
    return True


def eligible(character, row):
    if not supported(row):
        raise ValueError('该技能的特殊效果尚未恢复。')
    spell, condition = row['spell'], row['learn']
    if condition['sch'] != bytes.fromhex(character['physique'])[0]:
        raise ValueError('不能学习其他流派的技能。')
    if character['level'] < spell.get('req_level', 1):
        raise ValueError('角色等级尚未达到该技能的学习要求。')
    school_skills = character['world_state'].get('school_skills', {})
    if any(school_skills.get(str(identifier), 0) < level
           for identifier, level in condition.get('sch_skill', [])):
        raise ValueError('请先提升对应的流派修炼技能。')
    if condition.get('sch_score', 0):
        raise ValueError('该技能需要尚未恢复的流派贡献。')


def reconcile(character):
    """Restore only eligible free first ranks; never buy/upgrade a skill."""
    additions = []
    for identifier, ranks in catalog()['ranks'].items():
        row = ranks.get('1')
        if not row or row['learn'].get('cash', 0) or identifier in learned(character):
            continue
        try:
            eligible(character, row)
        except ValueError:
            continue
        character['world_state'].setdefault(STATE_KEY, {})[identifier] = 1
        additions.append(int(identifier))
    return additions


def repair(session):
    return character_store.transact(session, reconcile)


def messages(session, character):
    # roleSpells/spells are BASE properties, so login must explicitly stream
    # each learned rank into the original client Skill singleton.
    return [avatar_client_message(session, 'inform_role_spell', {'id': int(identifier), 'level': level})
            for identifier, level in sorted(learned(character).items(), key=lambda item: int(item[0]))
            if supported(rank(identifier, level))]


def learn(session, identifier, npc=None):
    from attribute_service import notice
    if not session.world_ready or getattr(session, 'battle_id', ''):
        return [notice(session, '请在战斗结束后学习技能。')] if session.world_ready else []
    if npc:
        from battle_service import selected
        master = catalog().get('masters', {}).get(str(npc['template_id']), {})
        character = selected(session)
        if (character is None or master.get('school') != bytes.fromhex(character['physique'])[0]
                or sum((session.position[i] - npc['position'][i]) ** 2 for i in (0, 2)) > 25 ** 2):
            return [notice(session, '请靠近本流派技能大师。')]
    upgraded = []

    def change(character):
        previous = learned(character).get(str(identifier), 0)
        row = rank(identifier, previous + 1)
        if not row:
            raise ValueError('该技能已达当前最高等级，或不是可学习的角色技能。')
        eligible(character, row)
        cost = row['learn'].get('cash', 0)
        if character['currency']['cash'] < cost:
            raise ValueError('学习技能所需金钱不足。')
        character['currency']['cash'] -= cost
        character['world_state'].setdefault(STATE_KEY, {})[str(identifier)] = previous + 1
        upgraded.append({'id': identifier, 'level': previous + 1})

    try:
        character = character_store.transact(session, change)
    except ValueError as error:
        return [notice(session, str(error))]
    if character is None or not upgraded:
        return []
    LOG.info('Role skill learned and saved: skill=%d rank=%d', identifier, upgraded[0]['level'])
    return [avatar_client_message(session, 'update_role_spell', upgraded[0]),
            avatar_client_message(session, 'update_cash', character['currency']['cash'])]


def effective_attributes(attributes, battle, sid):
    result = dict(attributes)
    for state in battle.get('skill_states', []):
        if state['sid'] == sid and state['expires'] >= battle['round']:
            for key, amount in state['changes'].items():
                result[key] = result.get(key, 0) + amount
    return result


def cast(character, battle, identifier, target, enhanced, operations, *,
         caster_sid=1, caster_attrs=None, friendly_attrs=None, cooldowns=None):
    """Apply a validated turn inside the same transaction as HP/MP and rewards."""
    from attribute_service import combat_attributes
    if enhanced:
        raise ValueError('蓄技点强化尚未恢复，请取消强化后施放。')
    level = learned(character).get(str(identifier), 0)
    row = rank(identifier, level)
    if not level or not supported(row):
        raise ValueError('该技能尚未学习或效果尚未恢复。')
    eligible(character, row)
    spell = row['spell']
    friendly = spell['target_type'] in (1, 4)
    if not friendly and target != 2:
        raise ValueError('请选择敌方目标。')
    if friendly and target != caster_sid and spell['target_type'] == 1:
        raise ValueError('该技能只能对自己施放。')
    if cooldowns is None:
        cooldowns = battle.get('skill_cooldowns', {})
    if cooldowns.get(str(identifier), 0) > battle['round']:
        raise ValueError('该技能还在冷却中。')
    base_attrs = combat_attributes(character) if caster_attrs is None else caster_attrs
    if friendly:
        if target == caster_sid:
            friendly_attrs = base_attrs
        elif friendly_attrs is None and caster_sid == 1:
            from companion_service import patient
            if patient(battle, target) is not None:
                friendly_attrs = patient(battle, target)
            elif target == 3 and battle.get('summon_joined'):
                from summon_progression import participant
                pet = participant(character, battle)
                friendly_attrs = pet['attributes']['combatAttr'] if pet else None
        if friendly_attrs is None or friendly_attrs.get('pro100', 0) <= 0:
            raise ValueError('请选择存活的友方目标。')
    attrs = effective_attributes(base_attrs, battle, caster_sid)
    consume = spell['consume']
    # Recovered SkillLevel.getMPConsume: int((mp + fix * character_level) * pro73).
    mp = max(0, int((consume['mp'] + consume['fix'] * character['level']) * attrs['pro73']))
    hp = max(0, int(consume.get('hp', 0) * attrs['pro89']))
    if base_attrs['pro101'] < mp or base_attrs['pro100'] <= hp:
        raise ValueError('法力或生命不足，请先补充。')
    base_attrs['pro101'] -= mp
    base_attrs['pro100'] -= hp
    operations.append(('battle_skill', caster_sid, target, identifier, level))
    if mp:
        operations.extend([('battle_mp_change', caster_sid, -mp),
                           ('battle_set_combat_attr', caster_sid, 'pro101', base_attrs['pro101'])])
    if hp:
        operations.extend([('battle_hp_change', caster_sid, -hp),
                           ('battle_set_combat_attr', caster_sid, 'pro100', base_attrs['pro100'])])
    enemy = effective_attributes({}, battle, 2)
    for effect in spell.get('effect', {}).values():
        args, formula = effect['args'], effect['formula']
        attack = attrs.get('pro31' if formula == 11 else 'pro41', 0)
        amount = max(1, math.floor(abs(args.get('fix', 0) + args.get('a', 0) * attack)))
        if friendly:
            amount = min(amount, friendly_attrs['pro7'] - friendly_attrs['pro100'])
            friendly_attrs['pro100'] += amount
            current = friendly_attrs['pro100']
        else:
            amount = min(battle['enemy_hp'], max(1, amount - int(enemy.get('pro32' if formula == 11 else 'pro42', 0))))
            battle['enemy_hp'] -= amount
            amount = -amount
            current = battle['enemy_hp']
        operations.extend([('battle_hp_change', target, amount),
                           ('battle_set_combat_attr', target, 'pro100', current)])
    for key in ('buff', 'target_buff'):
        sid = caster_sid if key == 'buff' else target
        for state in spell.get(key, {}).values():
            rounds = int(state.get('round', 0))
            if rounds <= 0:
                continue  # Zero-duration original cleanse markers have no lasting effect.
            buff = catalog()['buffs']['%d:%d' % (state['id'], state['level'])]
            changes, periodic = {}, 0
            for effect in buff.get('effect', {}).values():
                args, formula = effect['args'], effect['formula']
                if formula == 1:
                    changes['pro' + effect['attr'][2:]] = args.get('fix', 0)
                elif formula == 16:
                    periodic += math.floor(args.get('fix', 0) + args.get('b', 0) * attrs.get('pro41', 0))
                else:
                    periodic += math.floor(args.get('fix', 0) + args.get('a', 0) * attrs.get('pro31' if formula == 11 else 'pro41', 0))
            states = battle.setdefault('skill_states', [])
            states[:] = [s for s in states if (s['id'], s['sid']) != (state['id'], sid)]
            # Companions act after this round's tick; start their periodic
            # effects next round so the exported six rounds yield six ticks.
            begins = battle['round'] + (1 if caster_sid != 1 and periodic else 0)
            states.append({'id': state['id'], 'sid': sid, 'changes': changes, 'periodic': periodic,
                           'begins': begins, 'expires': begins + rounds - 1})
    cooldown = spell.get('cd', 0)
    if cooldown:
        cooldowns[str(identifier)] = battle['round'] + cooldown
        if caster_sid == 1:
            battle['skill_cooldowns'] = cooldowns
        operations.append(('battle_skill_cooldown', caster_sid, identifier, cooldown))
    if not battle['enemy_hp']:
        operations.append(('battle_die', 2))
    operations.append(('battle_motion_end',))
    return base_attrs


def periodic_effects(attrs, battle, operations, character=None):
    """Resolve bounded health ticks before victory and retaliation settlement."""
    for state in battle.get('skill_states', []):
        if (state['expires'] < battle['round'] or not state['periodic']
                or state.get('begins', 0) > battle['round']
                or state.get('last_tick') == battle['round']):
            continue
        state['last_tick'] = battle['round']
        sid = state['sid']
        patient = attrs if sid == 1 else None
        from companion_service import patient as companion_patient
        if companion_patient(battle, sid) is not None:
            patient = companion_patient(battle, sid)
        elif sid == 3 and character is not None and battle.get('summon_joined'):
            from summon_progression import participant
            pet = participant(character, battle)
            patient = pet['attributes']['combatAttr'] if pet else None
        if sid != 2 and patient is None:
            continue
        current = patient['pro100'] if patient is not None else battle['enemy_hp']
        from warrior_codec import catalog as battle_catalog
        maximum = patient['pro7'] if patient is not None else battle_catalog()['simulator_rules']['enemy_hp']
        if current <= 0:
            continue
        value = min(maximum, max(0, current + state['periodic']))
        if patient is not None:
            patient['pro100'] = value
        else:
            battle['enemy_hp'] = value
        operations.extend([('battle_hp_change', sid, value - current),
                           ('battle_set_combat_attr', sid, 'pro100', value)])
        if not value:
            operations.append(('battle_die', sid))
        operations.append(('battle_motion_end',))
