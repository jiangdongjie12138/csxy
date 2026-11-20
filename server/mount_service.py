"""Learn and ride the original client mounts with atomic durable ownership."""
import copy
import functools
import json
import time

import character_store
from attribute_service import notice, combat_attributes
from csxy_protocol import ROOT
from entity_methods import avatar_client_message, avatar_property_message

NONE = 255  # RidesData uses -1 locally; the native wire field is UINT8.
CAPACITY = 100
BASE_SPEED = 8.0


class MountRequestError(ValueError):
    pass


@functools.lru_cache(maxsize=1)
def catalog():
    return json.loads((ROOT / 'server/data/mounts.json').read_text('utf-8'))


def collection(character):
    """Validate saved records without discarding ownership or reindexing slots."""
    data = character.get('world_state', {}).get('rides', {})
    if not isinstance(data, dict):
        raise MountRequestError('坐骑存档格式异常。')
    rides, indices, identifiers = [], set(), set()
    for ride in data.get('rides', []):
        if (not isinstance(ride, dict) or type(ride.get('index')) is not int
                or not 0 <= ride['index'] < CAPACITY
                or str(ride.get('id')) not in catalog()
                or type(ride.get('expired')) is not int or not 0 <= ride['expired'] < 2 ** 32
                or ride['index'] in indices or ride['id'] in identifiers):
            raise MountRequestError('坐骑存档记录异常。')
        indices.add(ride['index'])
        identifiers.add(ride['id'])
        rides.append({key: ride[key] for key in ('index', 'id', 'expired')})
    active = data.get('active', NONE)
    if active not in indices:
        active = NONE
    return {'active': active, 'rides': sorted(rides, key=lambda r: r['index'])}


def active_ride(character, now=None):
    data = collection(character)
    now = time.time() if now is None else now
    ride = next((r for r in data['rides'] if r['index'] == data['active']), None)
    if ride and (not ride['expired'] or ride['expired'] > now):
        return ride
    return None


def visible_aspect(character):
    # Overlay only on the wire. Equipment writes the original raw appearance;
    # learning/riding never replaces an armour or transformation model.
    from equipment_service import aspect_info
    aspect = aspect_info(character)
    ride = active_ride(character)
    if ride:
        aspect['horse'] = catalog()[str(ride['id'])]['horse_aspect']
    return aspect


def ready(session, character=None):
    if (getattr(session, 'superseded', False) or not session.world_ready
            or getattr(session, 'battle_id', '') or getattr(session, 'scene_transfer', {})
            or getattr(session, 'scene_messages', []) or getattr(session, 'tour_running', False)):
        return False
    return not character or character.get('world_state', {}).get('tutorial_battle', {}).get('phase') not in (
        'preparing', 'input', 'animating')


def visual_messages(session, character, previous=None):
    ride = active_ride(character)
    current = catalog()[str(ride['id'])] if ride else None
    old = catalog()[str(previous['id'])] if previous else None
    replies = []
    if old and (not current or old['state_id'] != current['state_id']):
        replies.append(avatar_client_message(session, 'remove_state', old['state_id']))
    if current:
        replies.append(avatar_client_message(session, 'update_state', current['state_id'],
                                             current['state_level'], -1, {}))
    speed = min(BASE_SPEED * (1 + current['move_speed']), current['limit_speed']) if current else BASE_SPEED
    replies.extend([avatar_property_message(session, 'aspect', visible_aspect(character)),
                    avatar_client_message(session, 'ride_active', ride['index'] if ride else NONE),
                    avatar_client_message(session, 'set_speed', speed)])
    return replies


def messages(session, character):
    data = collection(character)
    if not active_ride(character):
        data['active'] = NONE
    return [avatar_client_message(session, 'ride_all', data)] + visual_messages(session, character)


def inform(session):
    if getattr(session, 'superseded', False) or not session.world_ready:
        return []
    character = next((c for c in character_store.characters(session)
                      if c['dbid'] == session.selected_character), None)
    return [avatar_client_message(session, 'ride_all', collection(character))] if character else []


def use_item(session, page, pos):
    from inventory_service import delta_messages
    current = next((c for c in character_store.characters(session)
                    if c['dbid'] == session.selected_character), None)
    item = next((i for i in (current or {}).get('inventory', [])
                 if (i['page'], i['pos']) == (page, pos)), None)
    definition = catalog().get(str((item or {}).get('id')))
    if not definition:
        return False, []
    if not ready(session, current):
        return True, []
    replies = []

    def change(character):
        if not ready(session, character):
            return
        selected = next((i for i in character['inventory']
                         if (i['page'], i['pos'], i['uuid']) == (page, pos, item['uuid'])), None)
        if not selected or selected['amount'] < 1:
            return
        if page != 0:
            raise MountRequestError('请在主背包中使用坐骑道具。')
        if character['level'] < definition['req_minlv']:
            raise MountRequestError('需要达到%d级才能学习该坐骑。' % definition['req_minlv'])
        if selected['attributes'].get('item_lock') or selected['attributes'].get('time_lock'):
            raise MountRequestError('请先解除坐骑道具锁定。')
        data = collection(character)
        if any(r['id'] == selected['id'] for r in data['rides']):
            raise MountRequestError('已经学会该坐骑，道具已保留。')
        indices = {r['index'] for r in data['rides']}
        if len(indices) >= CAPACITY:
            raise MountRequestError('坐骑栏已满，道具已保留。')
        before = copy.deepcopy(character)
        ride = {'index': next(i for i in range(CAPACITY) if i not in indices),
                'id': selected['id'], 'expired': 0}
        data['rides'].append(ride)
        character['world_state']['rides'] = data
        selected['amount'] -= 1
        if not selected['amount']:
            character['inventory'].remove(selected)
        replies.extend(delta_messages(session, before, character))
        replies.append(avatar_client_message(session, 'ride_one', ride))
        from quest_service import resync_item_objectives
        replies.extend(resync_item_objectives(session, character))
        replies.append(notice(session, '已学会新坐骑，可在坐骑栏中选择骑乘。', chat=True))

    try:
        character_store.transact(session, change)
    except MountRequestError as error:
        return True, [notice(session, str(error))]
    return True, replies


def ride_on(session, index):
    if not ready(session):
        return []
    replies = []

    def change(character):
        if not ready(session, character):
            return
        data = collection(character)
        ride = next((r for r in data['rides'] if r['index'] == index), None)
        if not ride:
            raise MountRequestError('尚未拥有该坐骑。')
        if ride['expired'] and ride['expired'] <= time.time():
            raise MountRequestError('该坐骑已过期。')
        if combat_attributes(character)['pro100'] <= 0:
            raise MountRequestError('倒地时不能骑乘。')
        if character['level'] < catalog()[str(ride['id'])]['req_minlv']:
            raise MountRequestError('角色等级不符合骑乘要求。')
        previous = active_ride(character)
        data['active'] = index
        character['world_state']['rides'] = data
        replies.extend(visual_messages(session, character, previous))

    try:
        character_store.transact(session, change)
    except MountRequestError as error:
        return [notice(session, str(error))]
    return replies


def clear_active(session, character):
    """Called inside the battle transaction; encoding must precede its commit."""
    data = collection(character)
    previous = active_ride(character)
    if data['active'] == NONE:
        return []
    data['active'] = NONE
    character['world_state']['rides'] = data
    return visual_messages(session, character, previous)


def restore_after_battle(session, character, battle, emit=True):
    """Restore only the same still-owned, valid mount; consume the intent once."""
    previous = battle.pop('resume_ride', None)
    if not previous:
        return []
    data = collection(character)
    ride = next((r for r in data['rides'] if r['index'] == previous['index']
                 and r['id'] == previous['id']), None)
    if (not ride or (ride['expired'] and ride['expired'] <= time.time())
            or combat_attributes(character)['pro100'] <= 0
            or character['level'] < catalog()[str(ride['id'])]['req_minlv']):
        return []
    data['active'] = ride['index']
    character['world_state']['rides'] = data
    return visual_messages(session, character) if emit else []


def ride_off(session):
    if not ready(session):
        return []
    replies = []

    def change(character):
        if ready(session, character):
            replies.extend(clear_active(session, character))

    character_store.transact(session, change)
    return replies
