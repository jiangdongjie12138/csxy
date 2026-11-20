"""Saved ordinary-spawn cooldown; task actors retain their quest lifecycle."""
import functools
import json
import time

from csxy_protocol import ROOT
from quest_catalog import catalog


@functools.lru_cache(maxsize=1)
def rules():
    return json.loads((ROOT / 'server/data/monster_lifecycle.json').read_text('utf-8'))


def ordinary(npc):
    born = catalog()['born'].get(str(npc['template_id']), {})
    return (npc['type'] == 'Monster' and born.get('reborn_protect_time', 0) > 0
            and born.get('max_life') != 0)


def available(character, npc, now=None):
    now = time.time() if now is None else now
    until = (character or {}).get('world_state', {}).get('monster_respawns', {}).get(str(npc['entity_id']), 0)
    return now >= until


def victory(character, battle, now):
    from npc_service import find_npc
    npc = find_npc(battle['npc'])
    if (battle.get('outcome') != 'victory' or not npc or not ordinary(npc)
            or battle.get('encounter', {}).get('story') or 'monster_respawn_at' in battle):
        return
    cooldowns = character['world_state'].setdefault('monster_respawns', {})
    # Prune only expired timers in this successful transaction, never in reads.
    cooldowns = {key: until for key, until in cooldowns.items() if until > now}
    until = now + rules()['simulator_respawn_seconds']
    cooldowns[str(npc['entity_id'])] = until
    character['world_state']['monster_respawns'] = cooldowns
    battle['monster_respawn_at'] = until
