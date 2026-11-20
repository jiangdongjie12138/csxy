"""Recovered StoreMan inventory and transaction handlers.

The original server exposes the store catalogue through the NPC's cell
methods.  This module keeps the catalogue read-only and commits purchases and
sales together with the character inventory, so a full bag or failed save
cannot charge the player without an item.
"""
import copy
import logging
import struct

import character_store
from entity_methods import avatar_client_message
from inventory_service import (BAG_SIZE, FIRST_BAG_PAGE, InventoryRequestError,
                               delta_messages, grant_items, intro_reward, sale_error,
                               item_info)

LOG = logging.getLogger('store')
PAGE_SIZE = 14  # const.STORE_ITEMS_PER_PAGE


def available(session, npc, character=None):
    if (not session.world_ready or getattr(session, 'battle_id', '')
            or getattr(session, 'superseded', False)
            or npc['entity_id'] not in session.visible_npcs or npc.get('type') != 'StoreMan'):
        return False
    if character is None:
        character = next((c for c in character_store.characters(session)
                          if c['dbid'] == session.selected_character), None)
    return bool(character and character.get('space_no') == npc.get('space_no')
                and sum((session.position[i] - npc['position'][i]) ** 2 for i in (0, 2)) <= 25 ** 2)


def spend(character, key, cost):
    if cost < 0 or character['currency'].get(key, 0) < cost:
        raise InventoryRequestError({'cash': '银两不足。', 'credit': '积分不足。',
                                     'tongbao': '通宝不足。'}.get(key, '货币不足。'))
    # Legacy/new saves may omit optional wallets such as mall credit. A
    # yuanbao-only cart still calls this with credit cost 0: leave that wallet
    # untouched instead of indexing a missing key after the balance check.
    if cost:
        character['currency'][key] = character['currency'].get(key, 0) - cost


def spend_yuanbao(character, cost):
    wallet = character['currency'].setdefault('yuanbao', {'pay': 0, 'free': 0})
    if cost < 0 or sum(int(wallet.get(k, 0)) for k in ('pay', 'free')) < cost:
        raise InventoryRequestError('元宝不足。')
    free = min(int(wallet.get('free', 0)), cost)
    wallet['free'] = int(wallet.get('free', 0)) - free
    wallet['pay'] = int(wallet.get('pay', 0)) - (cost - free)


def sync(session, before, after):
    from quest_service import item_objective_messages
    result = delta_messages(session, before, after)
    for key in ('tongbao', 'credit'):
        if before['currency'].get(key, 0) != after['currency'].get(key, 0):
            result.append(avatar_client_message(session, 'update_' + key, after['currency'].get(key, 0)))
    return result + item_objective_messages(session, after)


def enforce_hold(character, grants):
    from collections import Counter
    counts = Counter()
    for item in character['inventory'] + list(character['equipment'].values()):
        counts[item['id']] += item['amount']
    for identifier, amount in grants:
        counts[identifier] += amount
        maximum = intro_reward()['items'][str(identifier)].get('mhold', 0)
        if maximum and counts[identifier] > maximum:
            raise InventoryRequestError('超过该物品的最大持有数量。')


def _tables():
    from quest_controller import auxiliary
    return auxiliary().get('storeman_data', {})


def _store(npc):
    # world_npcs stores the store catalogue id as template_id for StoreMan;
    # keep an explicit storeman_id override for generated/event NPCs.
    row = _tables().get(str(npc.get('template_id')))
    if row is None:
        # NPC template IDs and store IDs coincide in the recovered world data;
        # a few maps use the table id directly.
        row = _tables().get(str(npc.get('storeman_id', npc.get('template_id'))))
    return row or {}


def _items(row):
    return [int(value) for value in row.get('items', [])]


def query(session, npc, page=0):
    if not available(session, npc) or not _store(npc):
        return []
    row = _store(npc)
    values = _items(row)
    total = max(1, (len(values) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(int(page), total - 1))
    start = page * PAGE_SIZE
    subset = values[start:start + PAGE_SIZE]
    from npc_service import npc_client_message
    return [npc_client_message(npc, 'storeQuery', total, page, subset)]


def buy(session, npc, item_id, amount):
    if not available(session, npc):
        return []
    row = _store(npc)
    item_id, amount = int(item_id), int(amount)
    if amount <= 0 or amount > 65535 or item_id not in _items(row):
        return []
    template = intro_reward()['items'].get(str(item_id))
    money_type = int(row.get('money_type', 1))
    if template is None:
        return []
    replies = []

    def change(character):
        if not available(session, npc, character):
            raise InventoryRequestError('请靠近商店后再交易。')
        before = copy.deepcopy(character)
        if money_type == 5:
            identifier, needed = template.get('buy_item', (0, 0))
            remaining = int(needed) * amount
            if remaining <= 0:
                raise InventoryRequestError('兑换价格尚未恢复。')
            for item in sorted(character['inventory'], key=lambda i: (i['page'], i['pos'])):
                if item['id'] != identifier or item['attributes'].get('item_lock') or item['attributes'].get('time_lock'):
                    continue
                taken = min(remaining, item['amount'])
                remaining -= taken
                item['amount'] -= taken
                if not remaining:
                    break
            if remaining:
                raise InventoryRequestError('兑换材料不足。')
            character['inventory'] = [i for i in character['inventory'] if i['amount']]
        else:
            key = {1: 'cash', 3: 'tongbao', 4: 'yuanbao'}.get(money_type)
            price = int(template.get('buy_' + str(key), 0))
            if not key or price <= 0:
                raise InventoryRequestError('商品价格尚未恢复。')
            if key == 'yuanbao':
                spend_yuanbao(character, price * amount)
            else:
                spend(character, key, price * amount)
        enforce_hold(character, [(item_id, amount)])
        grant_items(character, [(item_id, amount)])
        replies.extend(sync(session, before, character))

    try:
        character_store.transact(session, change)
    except (InventoryRequestError, ValueError) as error:
        from attribute_service import notice
        return [notice(session, str(error))]
    LOG.info('Store purchase committed: npc=%s item=%d amount=%d', npc.get('template_id'), item_id, amount)
    return replies


def sell(session, npc, page, pos, amount):
    if not available(session, npc) or not _store(npc):
        return []
    row = _store(npc)
    page, pos, amount = int(page), int(pos), int(amount)
    if not 0 <= page < 6 or not 0 <= pos < BAG_SIZE or amount <= 0:
        return []
    replies = []

    def change(character):
        if not available(session, npc, character):
            raise InventoryRequestError('请靠近商店后再交易。')
        before = copy.deepcopy(character)
        item = next((i for i in character['inventory']
                     if (i['page'], i['pos']) == (page, pos)), None)
        template = intro_reward()['items'].get(str((item or {}).get('id')))
        if item is None or amount > item['amount'] or template is None:
            raise InventoryRequestError('物品数量或位置已变化。')
        error = sale_error(item, template)
        if error:
            raise InventoryRequestError(error)
        price = int(template.get('sell_cash', 0))
        if price <= 0:
            raise InventoryRequestError('该物品不能出售。')
        sold = dict(copy.deepcopy(item), amount=amount)
        import uuid
        sold['uuid'] = uuid.uuid4().hex
        sold['sold_for'] = price * amount
        receipts = character['world_state'].setdefault('store_retrieves', [])
        if len(receipts) >= 40:
            raise InventoryRequestError('回购物品已满，请先回购。')
        receipts.append(sold)
        item['amount'] -= amount
        if not item['amount']:
            character['inventory'].remove(item)
        character['currency']['cash'] += price * amount
        if character['currency']['cash'] >= 2 ** 31:
            raise InventoryRequestError('银两已达到上限。')
        replies.extend(sync(session, before, character))
        replies.append(avatar_client_message(session, 'add_retrieve_item', item_info(sold)))

    try:
        character_store.transact(session, change)
    except (InventoryRequestError, ValueError) as error:
        from attribute_service import notice
        return [notice(session, str(error))]
    return replies


def retrieve(session, npc, token=None):
    if not available(session, npc) or not _store(npc):
        return []
    replies = []
    def change(character):
        if not available(session, npc, character):
            raise InventoryRequestError('请靠近商店后再交易。')
        before = copy.deepcopy(character)
        receipts = character['world_state'].setdefault('store_retrieves', [])
        rows = [i for i in receipts if token is None or i['uuid'] == token.hex()]
        if not rows:
            return
        spend(character, 'cash', sum(i['sold_for'] for i in rows))
        for item in rows:
            enforce_hold(character, [(item['id'], item['amount'])])
            grant_items(character, [(item['id'], item['amount'])], properties=item['attributes'])
            receipts.remove(item)
        replies.extend(sync(session, before, character))
        replies.extend(avatar_client_message(session, 'remove_retrieve_item', bytes.fromhex(i['uuid'])) for i in rows)
    try:
        character_store.transact(session, change)
    except ValueError as error:
        from attribute_service import notice
        return [notice(session, str(error))]
    return replies


def interact(session, npc, method, arguments):
    """Dispatch StoreMan cell methods after npc_service's visibility checks."""
    if method == 'query':
        if len(arguments) != 1:
            raise ValueError('store query needs PAGE')
        return query(session, npc, struct.unpack('<b', arguments)[0])
    if method == 'buy':
        if len(arguments) != 6:
            raise ValueError('store buy needs ITEM_ID and AMOUNT')
        return buy(session, npc, *struct.unpack('<IH', arguments))
    if method == 'sell':
        if len(arguments) != 4:
            raise ValueError('store sell needs PAGE, POS and AMOUNT')
        return sell(session, npc, *struct.unpack('<bbH', arguments))
    if method == 'retrieve':
        from csxy_protocol import packed_bytes
        token, end = packed_bytes(arguments, 0)
        if len(token) != 16 or end != len(arguments):
            raise ValueError('retrieve needs one UUID')
        return retrieve(session, npc, token)
    if method == 'retrieveAll':
        if arguments:
            raise ValueError('retrieveAll takes no arguments')
        return retrieve(session, npc)
    return []

