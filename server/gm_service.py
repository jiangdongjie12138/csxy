"""Offline GM edits with optimistic concurrency and exact pre-edit backups."""
from contextlib import contextmanager
import datetime
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import uuid

import character_store as store
from csxy_protocol import ROOT
from service_guard import service_lease

LIMITS = {'level': (1, 130), 'experience': (0, 2**31-1), 'cash': (0, 2**31-1),
          'binded_cash': (0, 2**31-1), 'tongbao': (0, 2**31-1),
          'yuanbao_pay': (0, 2**31-1), 'yuanbao_free': (0, 2**31-1), 'credit': (0, 65535)}
PORTS = ((socket.SOCK_STREAM, 8000), (socket.SOCK_STREAM, 20560),
         (socket.SOCK_STREAM, 28000), (socket.SOCK_DGRAM, 20560), (socket.SOCK_DGRAM, 28000))


def game_running():
    if os.name != 'nt':
        return False
    check = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
                            '@(Get-Process -Name csxy -ErrorAction SilentlyContinue).Count'],
                           capture_output=True, text=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    if check.returncode != 0 or not check.stdout.strip().isdigit():
        raise ValueError('无法确认游戏已退出，请稍后重试。')
    return int(check.stdout.strip()) > 0


@contextmanager
def offline_guard(root):
    with service_lease(root):
        if game_running():
            raise ValueError('请先退出游戏，再修改存档。')
        reserved = []
        try:
            # Also blocks old server versions without the lease, and holds the
            # endpoints throughout the edit so they cannot start mid-commit.
            for kind, port in PORTS:
                sock = socket.socket(socket.AF_INET, kind)
                reserved.append(sock)
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    sock.bind(('0.0.0.0', port))
                except OSError:
                    raise ValueError('服务端仍在运行，请先停止服务端再保存。') from None
            yield
        finally:
            for sock in reserved:
                sock.close()


def snapshot():
    if not store.PATH.exists():
        return {'characters': [], 'sha256': ''}
    raw = store.PATH.read_bytes()
    state, _ = store._decode(raw)
    result = []
    for account, characters in state.items():
        for character in characters:
            currency = character['currency']
            wallet = currency.get('yuanbao', {})
            values = {'level': character['level'], 'experience': character['experience'],
                      'cash': currency.get('cash', 0), 'binded_cash': currency.get('binded_cash', 0),
                      'tongbao': currency.get('tongbao', 0), 'credit': currency.get('credit', 0),
                      'yuanbao_pay': wallet.get('pay', 0), 'yuanbao_free': wallet.get('free', 0)}
            result.append({'account': account, 'account_name': bytes.fromhex(account).decode('gbk', 'replace'),
                           'dbid': character['dbid'],
                           'name': bytes.fromhex(character['name']).decode('gbk', 'replace'),
                           'values': values})
    return {'characters': result, 'sha256': hashlib.sha256(raw).hexdigest()}


def update(account, dbid, values, expected_sha256):
    if not isinstance(values, dict) or not values or set(values)-LIMITS.keys():
        raise ValueError('修改字段无效。')
    for key, value in values.items():
        low, high = LIMITS[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError('%s 超出允许范围 %d～%d。' % (key, low, high))
    root = store.PATH.resolve().parents[2]
    with offline_guard(root), store._LOCK:
        raw = store.PATH.read_bytes()
        if not expected_sha256 or hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError('存档已发生变化，请重新读取角色后再保存。')
        state, revision = store._decode(raw)
        character = next((row for row in state.get(account, []) if row['dbid'] == dbid), None)
        if character is None:
            raise ValueError('找不到选中的角色。')
        old_level = character['level']
        for key, value in values.items():
            if key in ('level', 'experience'):
                character[key] = value
            elif key.startswith('yuanbao_'):
                character['currency'].setdefault('yuanbao', {'pay': 0, 'free': 0})[key[8:]] = value
            else:
                character['currency'][key] = value
        if character['level'] != old_level:
            from growth_service import allocated, rules, reconcile
            if sum(allocated(character).values()) > (character['level']-1)*rules()['free_points_per_level']:
                raise ValueError('目标等级低于已有加点所需等级，请使用更高的等级。')
            reconcile(character, refill=True)
        character['updated_at'] = store._now()
        store._validate(state)
        stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        backup = store.PATH.parent/'gm_backups'/('characters_'+stamp+'_'+uuid.uuid4().hex[:8]+'.json')
        store._atomic_write(backup, raw)
        # Verify optimistic concurrency again immediately before the commit.
        if store.PATH.read_bytes() != raw:
            raise ValueError('存档在保存前变化，请重新读取。')
        store._write(state, revision)
        return {'backup': str(backup), 'character': character['dbid']}
