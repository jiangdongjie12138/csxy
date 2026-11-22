"""Offline login-format probes; does not launch BigWorld or change game saves.

The stock readers below are Python ports of the inspected C++ branches, not
compiled BigWorld code. Fixtures use a generated, in-memory RSA key and public
test values. The isolated simulator functions do not initialise Sessions.
"""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import socket
import struct
import sys
import types

from Crypto.Cipher import Blowfish, PKCS1_OAEP
from Crypto.PublicKey import RSA

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES = ROOT / "tmp/bigworld-compare-20261122"
MAGIC = 0xDEADBEEF
SOURCE_PATHS = {
    "1.9.1": {
        "interface": "bigworld/src/common/login_interface.hpp",
        "gate": "bigworld/src/server/loginapp/loginapp.cpp",
        "params": "bigworld/src/common/login_interface.cpp",
        "rsa": "src/lib/network/public_key_cipher.cpp",
        "encryption": "src/lib/network/encryption_filter.cpp",
        "key_policy": "src/lib/network/encryption_filter.hpp",
        "address": "src/lib/network/basictypes.cpp",
        "address_types": "src/lib/network/basictypes.hpp",
        "reply": "bigworld/src/common/login_interface.hpp",
        "base_login": "bigworld/src/common/servconn.cpp",
    },
    "2.0.1": {
        "interface": "src/lib/connection/login_interface.hpp",
        "gate": "src/server/loginapp/loginapp.cpp",
        "params": "src/lib/connection/log_on_params.cpp",
        "rsa": "src/lib/network/public_key_cipher.cpp",
        "encryption": "src/lib/network/encryption_filter.cpp",
        "key_policy": "src/lib/network/encryption_filter.hpp",
        "address": "src/lib/network/basictypes.cpp",
        "address_types": "src/lib/network/basictypes.hpp",
        "reply": "src/lib/connection/login_reply_record.hpp",
        "base_login": "src/lib/connection/baseapp_login_request.cpp",
    },
}


def isolated_protocol():
    path = ROOT / "server/csxy_protocol.py"
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "SESSIONS"
                for t in node.targets))]
    module = types.ModuleType("_login_probe_protocol")
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    exec(compile(tree, str(path), "exec"), module.__dict__)
    return module


def isolated_handshake(protocol, session):
    path = ROOT / "server/udp_baseapp_server.py"
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name in ("decode_session", "response_for")]
    namespace = {
        "struct": struct,
        "SESSIONS": types.SimpleNamespace(candidates=lambda peer: [session]),
        "decrypt_packet": protocol.decrypt_packet,
        "packet_parts": protocol.packet_parts,
        "reply_bundle": protocol.reply_bundle,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def packed(value):
    return (bytes([len(value)]) if len(value) < 255
            else b"\xff" + len(value).to_bytes(3, "little")) + value


def read_packed(data, offset):
    if offset >= len(data):
        raise ValueError("missing length")
    length = data[offset]
    offset += 1
    if length == 255:
        if offset + 3 > len(data):
            raise ValueError("short extended length")
        length = int.from_bytes(data[offset:offset + 3], "little")
        offset += 3
    if offset + length > len(data):
        raise ValueError("short packed value")
    return data[offset:offset + length], offset + length


def stock_params(plain, version):
    """Port only the well-formed, HAS_DIGEST input path needed by these probes."""
    offset = 1
    fields = []
    for _ in range(3):
        value, offset = read_packed(plain, offset)
        fields.append(value)
    if plain[0] & 1:
        if offset + 16 > len(plain):
            raise ValueError("short digest")
        offset += 16
    if version == "1.9.1":
        # Faithful to the two optional read/else branches in this snapshot:
        # a single nonce is read, then reset to zero by the second else.
        nonce = 0
        for _ in range(2):
            if offset < len(plain):
                nonce = struct.unpack_from("<I", plain, offset)[0]
                offset += 4
            else:
                nonce = 0
    else:
        nonce = struct.unpack_from("<I", plain, offset)[0]
        offset += 4
    return fields, nonce, len(plain) - offset


def stock_encrypt_blocks(key, plain):
    assert len(plain) % 8 == 0
    cipher = Blowfish.new(key, Blowfish.MODE_ECB)
    previous = bytes(8)
    result = bytearray()
    for offset in range(0, len(plain), 8):
        block = plain[offset:offset + 8]
        result.extend(cipher.encrypt(bytes(x ^ y for x, y in zip(block, previous))))
        previous = block
    return bytes(result)


def stock_decrypt_blocks(key, encrypted):
    if not encrypted or len(encrypted) % 8:
        raise ValueError("invalid ciphertext length")
    cipher = Blowfish.new(key, Blowfish.MODE_ECB)
    previous = bytes(8)
    result = bytearray()
    for offset in range(0, len(encrypted), 8):
        decoded = cipher.decrypt(encrypted[offset:offset + 8])
        block = bytes(x ^ y for x, y in zip(decoded, previous))
        result.extend(block)
        previous = block
    return bytes(result)


def stock_encrypt_packet(key, data):
    wastage = 8 - ((len(data) + 4) % 8)
    # Native code need not zero all padding; zero is a valid probe fixture.
    clear = data + bytes(wastage - 1) + struct.pack("<I", MAGIC) + bytes([wastage])
    return stock_encrypt_blocks(key, clear)


def stock_decrypt_packet(key, encrypted):
    clear = stock_decrypt_blocks(key, encrypted)
    if len(clear) < 5 or struct.unpack_from("<I", clear, len(clear) - 5)[0] != MAGIC:
        raise ValueError("invalid ENCRYPTION_MAGIC")
    wastage = clear[-1]
    if not 1 <= wastage <= 8 or wastage + 4 > len(clear):
        raise ValueError("invalid wastage")
    return clear[:-(wastage + 4)]


def verify_sources(root):
    contents, manifest = {}, []
    for version, mapping in SOURCE_PATHS.items():
        tree = json.loads((root / (version + "-tree.json")).read_text("utf-8"))
        assert not tree.get("truncated"), "source inventory was truncated"
        index = {item["path"]: item for item in tree["tree"]}
        contents[version] = {}
        for role, path in mapping.items():
            data = (root / version / path).read_bytes()
            digest = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            assert digest == index[path]["sha"], "source inventory mismatch: " + path
            contents[version][role] = data.decode("latin1")
            if not any(x["version"] == version and x["path"] == path for x in manifest):
                manifest.append({"version": version, "path": path, "git_blob_sha1": digest,
                                 "url": f"https://github.com/v2v3v4/BigWorld-Engine-{version}/blob/master/{path}"})
    for version, source in contents.items():
        assert "RSA_PKCS1_OAEP_PADDING" in source["rsa"]
        assert "ENCRYPTION_MAGIC = 0xdeadbeef" in source["encryption"]
        assert "os.insertRaw( a.ip )" in source["address"]
        assert "os.insertRaw( a.port )" in source["address"]
        assert "os << a.salt" in source["address"]
        assert "uint32\t\t\t\tsessionKey" in source["reply"]
        assert "replyRecord().sessionKey << attempt_" in source["base_login"]
    assert "if (version != LOGIN_VERSION)" in contents["1.9.1"]["gate"]
    assert "version < OLDEST_SUPPORTED_CLIENT_LOGIN_VERSION" in contents["2.0.1"]["gate"]
    assert "LOGIN_VERSION < version" in contents["2.0.1"]["gate"]
    assert contents["1.9.1"]["params"].count("*pRawStream << nonce_") == 2
    assert contents["1.9.1"]["params"].count("nonce_ = 0;") == 2
    return contents, manifest


def run(sources):
    source, manifest = verify_sources(sources)
    protocol = isolated_protocol()
    checks = []

    def check(name, observed, expected):
        assert observed == expected, (name, observed, expected)
        checks.append({"name": name, "observed": observed, "expected": expected, "passed": True})

    # All credential/key material here is generated or public test data.
    private = RSA.generate(1024)
    protocol.private_key = lambda: private
    key = bytes(range(16))
    username, password = b"compat_probe", b"test-only"
    nonce = 0x12345678
    plain = (b"\x01" + packed(username) + packed(password) + packed(key)
             + bytes(16) + struct.pack("<I", nonce))
    rsa_cipher = PKCS1_OAEP.new(private.public_key()).encrypt(plain)
    reply_id = bytes.fromhex("11223344")
    request = (struct.pack("<HBH", 1, 0, 132) + reply_id + bytes(2)
               + struct.pack("<I", 43) + rsa_cipher + struct.pack("<H", 2))
    check("csxy_login_request_bytes", len(request), 145)
    check("csxy_actual_parser_accepts_synthetic_request",
          protocol.login_params(request) == (username, key), True)
    decoded = PKCS1_OAEP.new(private).decrypt(request[15:143])
    check("rsa_oaep_roundtrip", decoded == plain, True)

    rules = {}
    for version in SOURCE_PATHS:
        text = source[version]["interface"]
        if version == "1.9.1":
            current = int(re.search(r"#define\s+LOGIN_VERSION\s+uint32\(\s*(\d+)", text)[1])
            minimum = current
        else:
            current = int(re.search(r"const uint32 LOGIN_VERSION = (\d+)", text)[1])
            minimum = int(re.search(r"OLDEST_SUPPORTED_CLIENT_LOGIN_VERSION = (\d+)", text)[1])
        client_version = struct.unpack_from("<I", request, 11)[0]
        check(version + ":stock_gate_accepts_43", minimum <= client_version <= current, False)
        check(version + ":stock_gate_accepts_own_version", minimum <= current <= current, True)
        fields, read_nonce, remaining = stock_params(decoded, version)
        check(version + ":login_fields_decodable_after_gate_override", fields == [username, password, key], True)
        check(version + ":single_nonce_reader_value", read_nonce, 0 if version == "1.9.1" else nonce)
        check(version + ":single_nonce_remaining_bytes", remaining, 0)
        rules[version] = {"login_minimum": minimum, "login_maximum": current,
                          "accepts_43": False,
                          "nonce_preserved": read_nonce == nonce,
                          "stock_login_compatible": False}
    check("1.9.1:double_nonce_control",
          stock_params(decoded + struct.pack("<I", nonce), "1.9.1")[1], nonce)

    token = 0x10203040
    record = protocol.login_record("127.0.0.1", 28000, token)
    stock_record = socket.inet_aton("127.0.0.1") + struct.pack("!H", 28000) + struct.pack("<HI", 0, token)
    check("csxy_success_record_bytes", len(record), 20)
    check("stock_success_record_bytes", len(stock_record), 12)
    check("common_address_and_token_prefix", record[:12] == stock_record, True)
    stock_record_cipher = stock_encrypt_blocks(key, stock_record + bytes(4))
    check("stock_success_record_cipher_bytes", len(stock_record_cipher), 16)
    check("stock_decrypted_record_has_required_20_bytes",
          len(protocol.decrypt_stream(key, stock_record_cipher)) >= 20, False)
    cipher_record = protocol.encrypt_stream(key, record)
    check("csxy_success_record_cipher_bytes", len(cipher_record), 24)
    check("same_blowfish_stream_algorithm",
          cipher_record == stock_encrypt_blocks(key, record + bytes(4)), True)
    login_reply = protocol.reply_bundle(reply_id, b"\x01" + cipher_record)
    check("csxy_login_reply_bytes", len(login_reply), 36)
    check("csxy_record_roundtrip", protocol.decrypt_stream(key, login_reply[12:])[:20] == record, True)

    session = types.SimpleNamespace(key=key, login_id=token, base_peer=None)
    handshake = isolated_handshake(protocol, session)
    base_request = (struct.pack("<HBH", 1, 0, 8) + reply_id + bytes(2)
                    + struct.pack("<IIH", token, 0, 2))
    found, clear, parts, login = handshake["decode_session"](base_request, ("127.0.0.1", 12345))
    check("baseapp_request_bytes", len(base_request), 21)
    check("baseapp_request_application_arguments_bytes", len(parts["body"][9:]), 8)
    check("baseapp_token_and_attempt_decode", login == (reply_id, 0) and found is session, True)
    bad = bytearray(base_request)
    struct.pack_into("<I", bad, 11, token + 1)
    try:
        handshake["decode_session"](bytes(bad), ("127.0.0.1", 12345))
        rejected = False
    except ValueError:
        rejected = True
    check("baseapp_wrong_token_rejected", rejected, True)
    base_reply = handshake["response_for"](session, parts, login)
    check("baseapp_reply_clear_bytes", len(base_reply), 15)
    csxy_packet = protocol.encrypt_packet(key, base_reply)
    stock_packet = stock_encrypt_packet(key, base_reply)
    check("baseapp_csxy_reply_wire_bytes", len(csxy_packet), 16)
    check("same_clear_reply_stock_filter_wire_bytes", len(stock_packet), 24)
    check("csxy_filter_roundtrip", protocol.decrypt_packet(key, csxy_packet) == base_reply, True)
    check("stock_filter_roundtrip", stock_decrypt_packet(key, stock_packet) == base_reply, True)
    try:
        stock_decrypt_packet(key, csxy_packet)
        magic_rejected = False
    except ValueError as error:
        magic_rejected = str(error) == "invalid ENCRYPTION_MAGIC"
    check("stock_filter_rejects_csxy_packet_magic", magic_rejected, True)
    csxy_decoded_stock_packet = protocol.decrypt_packet(key, stock_packet)
    check("csxy_filter_keeps_four_extra_bytes",
          len(csxy_decoded_stock_packet) - len(base_reply), 4)
    check("csxy_filter_does_not_restore_stock_packet_exactly",
          csxy_decoded_stock_packet == base_reply, False)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "source audit and isolated synthetic packet probes; stock readers are Python ports",
        "native_bigworld_engine_executed": False,
        "real_client_capture_replayed": False,
        "real_game_save_or_session_read": False,
        "game_server_source_modified": False,
        "checks_passed": len(checks), "checks_failed": 0,
        "versions": rules,
        "checks": checks,
        "sources": manifest,
        "simulator_sources": [
            {"path": p, "sha256": hashlib.sha256((ROOT / p).read_bytes()).hexdigest()}
            for p in ("server/csxy_protocol.py", "server/udp_baseapp_server.py")],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--output", type=Path, default=ROOT / "analysis/login_compatibility_results.json")
    args = parser.parse_args()
    result = run(args.sources)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps({"checks_passed": result["checks_passed"],
                      "checks_failed": result["checks_failed"],
                      "native_bigworld_engine_executed": False,
                      "stock_login_compatible": {v: False for v in SOURCE_PATHS},
                      "output": str(args.output)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
