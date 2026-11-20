"""Atomic opening for containers with recovered deterministic contents."""
import copy
import functools
import json
import logging

import character_store
from attribute_service import notice
from csxy_protocol import ROOT
from inventory_service import (FIRST_BAG_PAGE, intro_reward, grant_items, delta_messages,
                               InventoryFullError, InventoryRequestError)

LOG = logging.getLogger('item-box')


@functools.lru_cache(maxsize=1)
def rules():
    return json.loads((ROOT / 'server/data/item_boxes.json').read_text('utf-8'))


def use_box(session, page, pos):
    current = next((c for c in character_store.characters(session)
                    if c['dbid'] == session.selected_character), None)
    item = next((i for i in (current or {}).get('inventory', [])
                 if (i['page'], i['pos']) == (page, pos)), None)
    template = intro_reward()['items'].get(str((item or {}).get('id')), {})
    if (template.get('type'), template.get('stype')) != (4, 1):
        return False, []
    if (getattr(session, 'superseded', False) or not session.world_ready
            or getattr(session, 'battle_id', '') or getattr(session, 'scene_transfer', {})
            or getattr(session, 'scene_messages', [])):
        return True, []
    replies = []
    definition = rules()['boxes'].get(str(item['id']))
    if not definition:
        return True, [notice(session, '该宝箱的奖励内容尚未恢复，宝箱已保留。')]

    def change(character):
        if character['world_state'].get('tutorial_battle', {}).get('phase') in (
                'preparing', 'input', 'animating'):
            return
        selected = next((i for i in character['inventory']
                         if (i['page'], i['pos'], i['uuid']) == (page, pos, item['uuid'])), None)
        if selected is None or selected['amount'] <= 0:
            return
        if page != FIRST_BAG_PAGE or not template.get('use_condition', 0) & 2:
            raise InventoryRequestError('请在主背包中打开宝箱。')
        if (character['level'] < template.get('req_minlv', 0)
                or character['level'] > template.get('req_maxlv', 255)):
            raise InventoryRequestError('角色等级不符合宝箱使用要求。')
        if selected['attributes'].get('item_lock') or selected['attributes'].get('time_lock'):
            raise InventoryRequestError('请先解除宝箱锁定。')
        before = copy.deepcopy(character)
        # Consume first so an emptied box slot can hold its contents. The
        # encompassing transaction rolls everything back when space is short.
        selected['amount'] -= 1
        if not selected['amount']:
            character['inventory'].remove(selected)
        # Bound contents cannot merge with previously unbound stacks.
        properties = {'ntrade': True} if template.get('ntrade') or selected['attributes'].get('ntrade') else {}
        grant_items(character, definition['items'], properties=properties)
        replies.extend(delta_messages(session, before, character))
        from quest_service import resync_item_objectives
        replies.extend(resync_item_objectives(session, character))
        rewards = '，'.join('%s × %d' % (intro_reward()['items'][str(n)]['name'], amount)
                           for n, amount in definition['items'])
        replies.append(notice(session, '打开%s，获得%s。' % (template['name'], rewards), chat=True))

    try:
        character = character_store.transact(session, change)
    except InventoryFullError:
        return True, [notice(session, '背包空间不足，请整理背包后再打开。')]
    except ValueError as error:
        return True, [notice(session, str(error))]
    if character is not None and replies:
        LOG.info('Item box committed: item=%d contents=%s', item['id'], definition['items'])
    return True, replies
