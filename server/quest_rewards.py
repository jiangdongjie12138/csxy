"""Explicit offline settlement for absent server-only reward libraries."""
import hashlib

from quest_catalog import definition

DROP_FIELDS = ('item_drop_lib', 'level_dependent_droplib', 'roll_droplib', 'roll_lv_droplib')


def drop_rewards(character, number, reward):
    from quest_controller import auxiliary
    rows, receipts = [], []
    level = max(1, min(130, character['level']))
    # All items below are original client templates.  Materials are graded by
    # level; odds and contents of the original libraries are unavailable.
    pool = (43502, 43505, 155088) if level < 20 else ((23023, 23036, 155089)
            if level < 50 else (23023, 23036, 155090))
    sources = [('quest', reward)]
    q = definition(number) or {}
    lib = auxiliary()['circle_quest_lib'].get(str(q.get('circle_quest_id')), {})
    if lib and (q.get('is_ring_end') or not q.get('prior_linked_quest')):
        sources.append(('circle', lib))
    for source, data in sources:
        for field in DROP_FIELDS:
            identifier = data.get(field)
            if not identifier:
                continue
            resolved = int(identifier)
            if field in ('level_dependent_droplib', 'roll_lv_droplib'):
                table = auxiliary()['level_dependent_droplib'].get(str(identifier), {}).get('drop', {})
                resolved = int(table.get(str(level // 10), 0))
            if not resolved:
                continue  # Original level band explicitly has no drop.
            generation = character.get('tasks', {}).get(str(number), {}).get('generation', '')
            seed = hashlib.sha256(('%s:%s:%s:%s:%s' % (number, generation, source, field, resolved)).encode()).digest()
            item = pool[int.from_bytes(seed[:4], 'little') % len(pool)]
            rows.append((item, 1))
            receipts.append({'source': source, 'field': field, 'library': int(identifier),
                             'resolved_library': resolved, 'level_band': level // 10,
                             'item': item, 'amount': 1, 'policy': 'simulator-supply-v1'})
    return rows, receipts


def circle_cash(character, number):
    """Reward formula IDs survive, but their server implementation does not."""
    from quest_controller import auxiliary
    q = definition(number) or {}
    lib = auxiliary()['circle_quest_lib'].get(str(q.get('circle_quest_id')), {})
    # The two original tutorial examples state their cash award in text.
    if number in (41061, 41062):
        return {'cash': 125, 'binded_cash': 0}, 'client-description-125-v1'
    values = {key: max(1, character['level']) * 10 if lib.get(key + '_formula_id') else 0
              for key in ('cash', 'binded_cash')}
    return values, 'simulator-circle-cash-v1' if any(values.values()) else None
