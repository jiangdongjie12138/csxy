"""Original mall catalogue, pagination and atomic self-purchases.

MALL_ITEMSET_INFO.type selects the catalogue (1/2/3/6/8); the trailing
UINT8 selects self/pay/present, NOT a currency. Prices never trust payout.
"""
import copy
import functools
import json
import struct

import character_store
from csxy_protocol import ROOT, packed_bytes
from entity_methods import avatar_client_message
from inventory_service import grant_items, intro_reward, InventoryRequestError
from store_service import enforce_hold, spend, spend_yuanbao, sync


@functools.lru_cache(maxsize=1)
def catalog():
    return json.loads((ROOT / 'server/data/mall_catalog.json').read_text('utf-8'))


def ready(session):
    return bool(session.world_ready and not getattr(session, 'battle_id', '')
                and not getattr(session, 'superseded', False))


def selected(session):
    return next((c for c in character_store.characters(session)
                 if c['dbid'] == session.selected_character), None)


def page_rows(rows, page, size=9):
    total = max(1, (len(rows) + size - 1) // size)
    page = max(0, min(int(page), total - 1))
    return rows[page * size:(page + 1) * size], page, total


def info(rows, flag=0):
    return [{'id': int(r['id']), 'flags': flag} for r in rows]


def query(session, name, arguments):
    if not ready(session):
        return []
    all_rows = sorted(catalog()['mall_item_all'].values(), key=lambda r: (r.get('rank', 0), r['id']))
    credit_rows = list(catalog()['mall_item_credit'].values())
    if name == 'queryMallHotItems':
        if arguments:
            raise ValueError(name + ' takes no arguments')
        return [avatar_client_message(session, 'mall_hot_items', info(all_rows[:6], 2))]
    if name in ('queryMallAllItems', 'searchMallAllItemsByKind'):
        if len(arguments) != 2:
            raise ValueError(name + ' needs UINT8, PAGE')
        kind, page = struct.unpack('<Bb', arguments)
        field = 'kind' if name == 'queryMallAllItems' else 'search_kind'
        rows = all_rows if kind == 255 else [r for r in all_rows if r.get(field) == kind]
        rows, page, total = page_rows(rows, page)
        return [avatar_client_message(session, 'mall_all_items', kind if field == 'kind' else 254,
                                      info(rows), page, total)]
    if name in ('searchMallAllItemsByName', 'searchMallCreditItems'):
        raw, end = packed_bytes(arguments, 0)
        if end != len(arguments):
            raise ValueError(name + ' needs STRING')
        needle = raw.decode('gbk', 'replace')
        credit = name == 'searchMallCreditItems'
        rows = [r for r in (credit_rows if credit else all_rows) if needle in r['item_name']]
        rows, page, total = page_rows(rows, 0)
        return [avatar_client_message(session, 'mall_credit_items' if credit else 'mall_all_items',
                                      254, info(rows), page, total)]
    if len(arguments) != 1:
        raise ValueError(name + ' needs PAGE')
    page = struct.unpack('<b', arguments)[0]
    if name == 'queryMallPackageItems':
        rows, page, total = page_rows(list(catalog()['mall_item_package'].values()), page, 3)
        return [avatar_client_message(session, 'mall_package_items', [r['id'] for r in rows], page, total)]
    if name == 'queryMallCreditItems':
        rows, page, total = page_rows(credit_rows, page)
        return [avatar_client_message(session, 'mall_credit_items', 1, info(rows), page, total)]
    if name == 'queryMallHistoryItems':
        history = (selected(session) or {}).get('world_state', {}).get('mall_history', [])
        table = catalog()['mall_item_all']
        rows, page, total = page_rows([table[str(i)] for i in history if str(i) in table], page)
        return [avatar_client_message(session, 'mall_history_items', info(rows, 4), page, total)]
    if name == 'queryMallNewItems':
        rows, page, total = page_rows(list(reversed(all_rows)), page)
        return [avatar_client_message(session, 'mall_new_items', info(rows, 1), page, total)]
    return []


def buy(session, payload):
    if not ready(session):
        return []
    if len(payload) < 13:
        raise ValueError('buyMallItems needs ARRAY, UINT8, DBID')
    count = struct.unpack_from('<I', payload)[0]
    if not 1 <= count <= 12 or 4 + count * 11 + 9 != len(payload):
        raise ValueError('malformed mall cart')
    rows = [struct.unpack_from('<BIHI', payload, 4 + i * 11) for i in range(count)]
    buy_mode, dbid = struct.unpack_from('<Bq', payload, 4 + count * 11)
    from attribute_service import notice
    if buy_mode != 1 or dbid != 0:
        return [notice(session, '当前离线商城支持为自己购买。')]
    replies = []

    def change(character):
        before = copy.deepcopy(character)
        totals = {'yuanbao': 0, 'credit': 0}
        grants, history, cart = [], [], []
        for kind, identifier, amount, payout in rows:
            table = {1: 'all', 2: 'all', 3: 'all', 6: 'credit', 8: 'package'}.get(kind)
            row = catalog().get('mall_item_' + str(table), {}).get(str(identifier))
            if row is None or not 0 < amount <= 65535:
                raise InventoryRequestError('商城商品或数量无效。')
            item_id = row['item_id']
            template = intro_reward()['items'][str(item_id)]
            currency = 'credit' if kind == 6 else 'yuanbao'
            unit = int(template.get('buy_' + currency, 0)) * int(row.get('package_num', 1))
            cost = unit * amount
            if unit <= 0 or payout != cost:
                raise InventoryRequestError('商品价格已变化，请重新打开商城。')
            if not row.get('min_lv', 0) <= character['level'] <= row.get('max_lv', 255):
                raise InventoryRequestError('尚未满足商品购买等级。')
            totals[currency] += cost
            if kind == 8:
                grants.extend((int(r[0]), int(r[2]) * amount) for r in row['details'])
            else:
                grants.append((item_id, amount * row.get('package_num', 1)))
                if kind in (1, 2, 3):
                    history.append(identifier)
            cart.append({'type': kind, 'id': identifier, 'amount': amount, 'payout': cost})
        enforce_hold(character, grants)
        spend_yuanbao(character, totals['yuanbao'])
        spend(character, 'credit', totals['credit'])
        grant_items(character, grants)
        # Original conversion ratio: const.MALL_YUANBAO_TO_CREDIT = 100.
        commerce = character['world_state'].setdefault('mall_accounting', {})
        old = int(commerce.get('yuanbao_spent', 0))
        new = old + totals['yuanbao']
        character['currency']['credit'] = character['currency'].get('credit', 0) + new // 100 - old // 100
        commerce['yuanbao_spent'] = new
        commerce.setdefault('orders', []).append({'cart': cart, 'currency': totals})
        commerce['orders'] = commerce['orders'][-100:]
        previous = character['world_state'].get('mall_history', [])
        character['world_state']['mall_history'] = list(dict.fromkeys(reversed(history))) + [i for i in previous if i not in history]
        character['world_state']['mall_history'] = character['world_state']['mall_history'][:9]
        replies.extend(sync(session, before, character))
        replies.append(avatar_client_message(session, 'buy_mall_items_ok'))
        replies.append(notice(session, '商城购买成功，物品已放入背包。', chat=True))

    try:
        character = character_store.transact(session, change)
    except ValueError as error:
        return [notice(session, str(error))]
    return replies if character is not None else []
