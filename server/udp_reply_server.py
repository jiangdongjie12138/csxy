"""CSXY LoginApp UDP listener with verified framing and a 20-byte record."""
import datetime
import logging
import os
from pathlib import Path
import socket
import threading
from evidence_journal import append_record
from capture_settings import OUT as CAPTURE_ROOT, write_packet

from csxy_protocol import (SESSIONS, check_resource_key, encrypt_stream,
                           login_key, login_params, login_record, reply_bundle)

OUT = CAPTURE_ROOT / 'login_frames'
MODE = os.environ.get('REPLY_MODE', 'login_ok')
LOG = logging.getLogger('login-udp')


def _request_bf_key(datagram, variant=None):
    return login_key(datagram)


_bf_stream_encrypt = encrypt_stream
_reply_bundle = reply_bundle


def build_reply(datagram, mode, peer=('127.0.0.1', 0)):
    if mode == 'none':
        return None
    username, key = login_params(datagram)
    rid = datagram[5:9]
    if mode == 'reply65':
        desc = b'Protocol test rejection.'
        return reply_bundle(rid, bytes([65, len(desc)]) + desc)
    if mode not in ('login_ok', 'login_ok_bf_full16'):
        raise ValueError('unsupported REPLY_MODE: ' + mode)
    session = SESSIONS.register(key, peer[0], username)
    SESSIONS.save()
    record = login_record(os.environ.get('BASEAPP_HOST', '127.0.0.1'),
                          int(os.environ.get('BASEAPP_PORT', '28000')), session.login_id)
    encrypted = encrypt_stream(key, record)
    if len(record) != 20 or len(encrypted) != 24:
        raise ValueError('invalid login-success record length')
    return reply_bundle(rid, b'\x01' + encrypted)


def main(done=None, on_ready=None):
    if MODE not in ('none', 'reply65', 'login_ok', 'login_ok_bf_full16'):
        raise ValueError('unsupported REPLY_MODE: ' + MODE)
    if MODE != 'none':
        check_resource_key()
    done = done or threading.Event()
    port = int(os.environ.get('LOGIN_PORT', os.environ.get('PORT', '20560')))
    bind = os.environ.get('LOGIN_BIND', '0.0.0.0')
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    seq = 0
    journal = OUT / ('udp_login_%s.manifest.jsonl' % ts)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((bind, port))
        sock.settimeout(0.5)
        LOG.info('Login UDP listening on %s:%d mode=%s', bind, sock.getsockname()[1], MODE)
        if on_ready:
            on_ready(sock.getsockname()[1])
        while not done.is_set():
            try:
                data, peer = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue
            seq += 1
            fn = 'udp_login_%s_%03d.bin' % (ts, seq)
            record = {'seq': seq, 'file': fn, 'size': len(data),
                      'peer': '%s:%d' % peer,
                      'time': datetime.datetime.now().isoformat(timespec='milliseconds')}
            try:
                write_packet(OUT / fn, data)
                reply = build_reply(data, MODE, peer)
                if reply:
                    sock.sendto(reply, peer)
                    response_file = fn.replace('.bin', '.reply.bin')
                    write_packet(OUT / response_file, reply)
                    record.update(reply_file=response_file, reply_size=len(reply))
                LOG.info('Login %s:%d received=%d reply=%d', *peer, len(data), len(reply or b''))
            except Exception as exc:
                record['error'] = type(exc).__name__ + ': ' + str(exc)
                LOG.exception('Login datagram rejected from %s:%d', *peer)
            record['reply_mode'] = MODE
            append_record(journal, record)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    main()
