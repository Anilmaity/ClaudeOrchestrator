"""Tests for the HTTP Basic Auth gate on the dashboard.

The real production credentials NEVER appear in this file. We freshly hash a
synthetic test password for every fixture so the live ``fleet.json`` secret
can't leak through a test snapshot.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import auth
import fleet


# --------------------------------------------------------------------------- #
# helpers — generate a fresh hash for the test user
# --------------------------------------------------------------------------- #
TEST_ITERATIONS = 50_000   # small enough that the test suite stays fast


def _hash_test(password: str, *, iterations: int = TEST_ITERATIONS):
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return {
        "salt_b64": base64.b64encode(salt).decode(),
        "hash_b64": base64.b64encode(h).decode(),
        "iterations": iterations,
    }


def _basic(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def cfg_with_auth(tmp_path, monkeypatch):
    """Temp fleet.json with auth enabled using a freshly hashed test password."""
    pdir = tmp_path / "p"
    pdir.mkdir()
    h = _hash_test("test-pw-not-real")
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps({
        "projects": [],
        "agents": [{"name": "alice", "role": "", "project_dir": str(pdir)}],
        "auth": {
            "enabled": True,
            "realm": "TestRealm",
            "username": "testuser",
            "salt_b64": h["salt_b64"],
            "hash_b64": h["hash_b64"],
            "iterations": h["iterations"],
            "algo": "pbkdf2_sha256",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(fleet, "CONFIG", path)
    monkeypatch.setattr(fleet, "AGENTS", fleet.load_config(path))
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "TASKS_FILE", tmp_path / "fleet_tasks.json")
    return path


@pytest.fixture
def cfg_no_auth(tmp_path, monkeypatch):
    """Temp fleet.json with NO auth block — gate stays open (legacy)."""
    pdir = tmp_path / "p"
    pdir.mkdir()
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps({
        "projects": [],
        "agents": [{"name": "alice", "role": "", "project_dir": str(pdir)}],
    }), encoding="utf-8")
    monkeypatch.setattr(fleet, "CONFIG", path)
    monkeypatch.setattr(fleet, "AGENTS", fleet.load_config(path))
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "TASKS_FILE", tmp_path / "fleet_tasks.json")
    return path


# --------------------------------------------------------------------------- #
# auth.py — pure helpers
# --------------------------------------------------------------------------- #
def test_verify_password_true_for_matching_creds():
    h = _hash_test("hunter2", iterations=10_000)
    assert auth.verify_password("hunter2", salt_b64=h["salt_b64"],
                                hash_b64=h["hash_b64"],
                                iterations=h["iterations"]) is True


def test_verify_password_false_for_wrong_password():
    h = _hash_test("hunter2", iterations=10_000)
    assert auth.verify_password("hunter3", salt_b64=h["salt_b64"],
                                hash_b64=h["hash_b64"],
                                iterations=h["iterations"]) is False


def test_verify_password_false_for_unknown_algo():
    h = _hash_test("x", iterations=10_000)
    assert auth.verify_password("x", salt_b64=h["salt_b64"],
                                hash_b64=h["hash_b64"],
                                iterations=h["iterations"],
                                algo="bcrypt") is False


def test_verify_password_false_for_malformed_b64():
    assert auth.verify_password("x", salt_b64="not!base64!",
                                hash_b64="alsonot", iterations=1) is False


def test_verify_password_false_for_nonpositive_iterations():
    h = _hash_test("x", iterations=10_000)
    assert auth.verify_password("x", salt_b64=h["salt_b64"],
                                hash_b64=h["hash_b64"],
                                iterations=0) is False


def test_parse_basic_header_valid():
    header = "Basic " + base64.b64encode(b"alice:wonderland").decode()
    assert auth.parse_basic_header(header) == ("alice", "wonderland")


def test_parse_basic_header_case_insensitive_scheme():
    header = "basic " + base64.b64encode(b"a:b").decode()
    assert auth.parse_basic_header(header) == ("a", "b")


def test_parse_basic_header_password_with_colon_is_preserved():
    header = "Basic " + base64.b64encode(b"alice:pass:word").decode()
    assert auth.parse_basic_header(header) == ("alice", "pass:word")


def test_parse_basic_header_none_cases():
    assert auth.parse_basic_header(None) is None
    assert auth.parse_basic_header("") is None
    assert auth.parse_basic_header("Bearer xyz") is None
    assert auth.parse_basic_header("Basic !!!notbase64") is None
    # base64 of a string with no colon -> rejected
    nocolon = "Basic " + base64.b64encode(b"nopassword").decode()
    assert auth.parse_basic_header(nocolon) is None


def test_load_auth_config_missing_returns_none():
    assert auth.load_auth_config({}) is None
    assert auth.load_auth_config({"auth": {"enabled": False,
                                            "username": "x",
                                            "salt_b64": "AA==",
                                            "hash_b64": "AA==",
                                            "iterations": 1}}) is None


def test_load_auth_config_validates_required_fields():
    # missing username
    bad = {"auth": {"enabled": True, "salt_b64": "AA==",
                    "hash_b64": "AA==", "iterations": 1}}
    assert auth.load_auth_config(bad) is None
    # zero iterations
    bad2 = {"auth": {"enabled": True, "username": "x",
                     "salt_b64": "AA==", "hash_b64": "AA==", "iterations": 0}}
    assert auth.load_auth_config(bad2) is None


def test_load_auth_config_returns_normalized_block():
    h = _hash_test("x")
    cfg = auth.load_auth_config({"auth": {
        "enabled": True, "username": "u",
        "salt_b64": h["salt_b64"], "hash_b64": h["hash_b64"],
        "iterations": h["iterations"]}})
    assert cfg["realm"] == "Claude Orchestrator"   # default applied
    assert cfg["algo"] == "pbkdf2_sha256"


def test_check_credentials_constant_time_username_mismatch():
    h = _hash_test("p")
    cfg = {"username": "alice", "salt_b64": h["salt_b64"],
           "hash_b64": h["hash_b64"], "iterations": h["iterations"]}
    assert auth.check_credentials("alice", "p", cfg) is True
    assert auth.check_credentials("bob", "p", cfg) is False
    assert auth.check_credentials("alice", "wrong", cfg) is False


# --------------------------------------------------------------------------- #
# fleet.load_auth — lock-guarded read
# --------------------------------------------------------------------------- #
def test_load_auth_reads_block_through_lock(cfg_with_auth):
    cfg = fleet.load_auth()
    assert cfg is not None
    assert cfg["username"] == "testuser"
    assert cfg["realm"] == "TestRealm"


def test_load_auth_returns_none_when_disabled(cfg_no_auth):
    assert fleet.load_auth() is None


# --------------------------------------------------------------------------- #
# HTTP integration — drive fleet.Handler over a real socket
# --------------------------------------------------------------------------- #
def _request(url, *, method="GET", headers=None, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    h = dict(headers or {})
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_unauthenticated_get_returns_401_with_www_authenticate(cfg_with_auth):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        for path in ("/", "/api/state"):
            status, headers, body = _request(base + path)
            assert status == 401, f"{path} should be 401"
            assert headers.get("WWW-Authenticate", "").startswith('Basic realm="TestRealm"')
            # body is generic — no leakage about which side failed
            assert b"Unauthorized" in body
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_correct_credentials_pass_through(cfg_with_auth):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        auth_h = {"Authorization": _basic("testuser", "test-pw-not-real")}
        status, _h, body = _request(base + "/api/state", headers=auth_h)
        assert status == 200
        st = json.loads(body)
        assert "agents" in st
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_wrong_password_returns_401(cfg_with_auth):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        bad = {"Authorization": _basic("testuser", "nope")}
        status, headers, _b = _request(base + "/api/state", headers=bad)
        assert status == 401
        assert "WWW-Authenticate" in headers
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_wrong_user_returns_401(cfg_with_auth):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        bad = {"Authorization": _basic("notme", "test-pw-not-real")}
        status, _h, _b = _request(base + "/api/state", headers=bad)
        assert status == 401
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_auth_disabled_bypasses_gate(cfg_no_auth):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # NO Authorization header — must pass straight through
        status, _h, body = _request(base + "/api/state")
        assert status == 200
        assert "agents" in json.loads(body)
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_healthz_and_logout_exempt_from_gate(cfg_with_auth):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # healthz is reachable without creds
        status, _h, body = _request(base + "/healthz")
        assert status == 200
        assert json.loads(body) == {"ok": True}

        # logout itself returns 401 with a DIFFERENT realm so the browser
        # invalidates the cached creds — but it answers regardless of auth.
        status, headers, _b = _request(base + "/logout")
        assert status == 401
        wa = headers.get("WWW-Authenticate", "")
        assert wa.startswith("Basic realm=")
        # must not reuse the production realm — that would let the cache stick
        assert "TestRealm" not in wa
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_whoami_returns_username_when_authenticated(cfg_with_auth):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        auth_h = {"Authorization": _basic("testuser", "test-pw-not-real")}
        status, _h, body = _request(base + "/api/whoami", headers=auth_h)
        assert status == 200
        whoami = json.loads(body)
        assert whoami == {"username": "testuser", "enabled": True}
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_whoami_when_auth_disabled_reports_null_username(cfg_no_auth):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        status, _h, body = _request(base + "/api/whoami")
        assert status == 200
        assert json.loads(body) == {"username": None, "enabled": False}
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_post_and_put_also_gated(cfg_with_auth, tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # POST without creds -> 401
        status, _h, _b = _request(base + "/api/tasks", method="POST",
                                  payload={"agent": "alice", "description": "x"})
        assert status == 401
        # PUT without creds -> 401
        status, _h, _b = _request(base + "/api/connectors/telegram",
                                  method="PUT", payload={"enabled": False})
        assert status == 401
    finally:
        srv.shutdown()
        t.join(timeout=5)


# --------------------------------------------------------------------------- #
# Regression: the LIVE fleet.json never gets the test password baked into it
# --------------------------------------------------------------------------- #
def test_live_fleet_json_never_contains_test_password():
    """Belt-and-suspenders: scan the repo's real fleet.json to confirm none of
    the test fixtures leaked into it. A typo in monkeypatching could otherwise
    overwrite the real config in CI."""
    real = Path(fleet.__file__).resolve().parent / "fleet.json"
    text = real.read_text(encoding="utf-8")
    for forbidden in ("test-pw-not-real", "hunter2", "testuser"):
        assert forbidden not in text, f"{forbidden!r} leaked into live config!"


# --------------------------------------------------------------------------- #
# Auth fast-path cache — skip the ~150ms PBKDF2 for an already-verified header
# --------------------------------------------------------------------------- #
def test_auth_cache_roundtrip_and_ttl():
    fleet._AUTH_CACHE.clear()
    cfg = {"hash_b64": "AAAA", "iterations": 1000}
    hdr = _basic("u", "p")
    assert fleet.auth_cache_lookup(hdr, cfg, now=1000.0) is None      # cold
    fleet.auth_cache_store(hdr, cfg, "u", now=1000.0)
    assert fleet.auth_cache_lookup(hdr, cfg, now=1000.0) == "u"        # hit
    assert fleet.auth_cache_lookup(
        hdr, cfg, now=1000.0 + fleet._AUTH_CACHE_TTL - 1) == "u"       # still valid
    assert fleet.auth_cache_lookup(
        hdr, cfg, now=1000.0 + fleet._AUTH_CACHE_TTL + 1) is None      # expired


def test_auth_cache_keyed_by_header_and_config():
    fleet._AUTH_CACHE.clear()
    cfg = {"hash_b64": "AAAA", "iterations": 1000}
    fleet.auth_cache_store(_basic("u", "p"), cfg, "u", now=0.0)
    # A different header (wrong password) is a different key -> miss.
    assert fleet.auth_cache_lookup(_basic("u", "WRONG"), cfg, now=0.0) is None
    # Same header but a rotated config (new hash) -> miss (no staleness).
    cfg2 = {"hash_b64": "BBBB", "iterations": 1000}
    assert fleet.auth_cache_lookup(_basic("u", "p"), cfg2, now=0.0) is None
    # No header -> never a hit.
    assert fleet.auth_cache_lookup(None, cfg, now=0.0) is None


def test_auth_cache_does_not_leak_to_wrong_password(cfg_with_auth):
    # After a valid request populates the cache, a wrong password (different
    # header) must STILL be rejected, and a repeat valid request still passes.
    fleet._AUTH_CACHE.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        good = {"Authorization": _basic("testuser", "test-pw-not-real")}
        assert _request(base + "/api/state", headers=good)[0] == 200    # populates cache
        bad = {"Authorization": _basic("testuser", "nope")}
        assert _request(base + "/api/state", headers=bad)[0] == 401      # wrong pw still 401
        assert _request(base + "/api/state", headers=good)[0] == 200     # cached hit still 200
    finally:
        srv.shutdown()
        t.join(timeout=5)
