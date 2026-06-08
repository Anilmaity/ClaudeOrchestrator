"""Tests for the off-platform notification connectors.

Three layers:
  1. The pure helpers in ``connectors.py`` (URL/payload shape, mask, dispatcher).
  2. The fleet.py HTTP endpoints (mask round-trip + PUT persistence under the
     T-04 _CONFIG_LOCK pattern).
  3. The ``_escalate_task`` fan-out (every enabled connector is invoked and a
     connector failure does NOT mask the original escalation).

Network is never touched: ``urllib.request.urlopen`` is patched out and the
test asserts on the URL + payload it would have sent.
"""
from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import connectors
import fleet


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _write_cfg(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config), encoding="utf-8")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Temp fleet.json with seeded telegram + whatsapp config (both enabled)."""
    pdir = tmp_path / "p"
    pdir.mkdir()
    path = tmp_path / "fleet.json"
    _write_cfg(path, {
        "projects": [],
        "agents": [{"name": "alice", "role": "", "project_dir": str(pdir)}],
        "connectors": {
            "telegram": {"enabled": True,
                         "bot_token": "111:secretAAAA1234",
                         "chat_id": "987654321"},
            "whatsapp": {"enabled": True,
                         "phone": "+919876543210",
                         "apikey": "abcd9999"},
        },
    })
    monkeypatch.setattr(fleet, "CONFIG", path)
    monkeypatch.setattr(fleet, "AGENTS", fleet.load_config(path))
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "TASKS_FILE", tmp_path / "fleet_tasks.json")
    monkeypatch.setattr(fleet, "NOTIFICATIONS_FILE",
                        tmp_path / "fleet_notifications.json")
    return path


class FakeResponse:
    def __init__(self, body=b'{"ok": true}', status=200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# --------------------------------------------------------------------------- #
# connectors.py — pure helpers
# --------------------------------------------------------------------------- #
def test_mask_secret_reveals_only_last_four():
    assert connectors.mask_secret("") == ""
    assert connectors.mask_secret("abc") == "***"
    assert connectors.mask_secret("abcd") == "***"
    assert connectors.mask_secret("topsecret1234") == "***1234"


def test_merge_with_defaults_backfills_missing():
    out = connectors.merge_with_defaults(None)
    assert set(out.keys()) == {"telegram", "whatsapp"}
    assert out["telegram"]["enabled"] is False
    out2 = connectors.merge_with_defaults({"telegram": {"enabled": True}})
    assert out2["telegram"]["enabled"] is True
    # the missing fields were backfilled (not None) so the dashboard can render
    assert out2["telegram"]["bot_token"] == ""
    assert out2["whatsapp"]["enabled"] is False


def test_validate_rejects_enable_without_credentials():
    with pytest.raises(ValueError):
        connectors.validate_connector(
            "telegram", {"enabled": True, "bot_token": "", "chat_id": ""})
    with pytest.raises(ValueError):
        connectors.validate_connector(
            "whatsapp", {"enabled": True, "phone": "", "apikey": ""})


def test_validate_strips_unknown_fields_and_keeps_known():
    out = connectors.validate_connector(
        "telegram",
        {"enabled": True, "bot_token": "t", "chat_id": "c",
         "rogue": "x"})
    assert "rogue" not in out
    assert out == {"enabled": True, "bot_token": "t", "chat_id": "c"}


def test_send_telegram_posts_to_bot_api(monkeypatch):
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = req.data
        seen["headers"] = dict(req.header_items())
        return FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    r = connectors.send_telegram("hello", bot_token="111:ABCDEF",
                                 chat_id="123456")
    assert r["ok"] is True
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.telegram.org/bot111:ABCDEF/sendMessage"
    payload = json.loads(seen["body"])
    assert payload == {"chat_id": "123456", "text": "hello",
                       "disable_web_page_preview": True}
    # request must declare JSON, otherwise telegram rejects it
    ct = {k.lower(): v for k, v in seen["headers"].items()}
    assert ct.get("content-type") == "application/json"


def test_send_telegram_rejects_missing_credentials():
    r = connectors.send_telegram("hi", bot_token="", chat_id="x")
    assert r["ok"] is False and "required" in r["error"]


def test_send_whatsapp_callmebot_url(monkeypatch):
    """We picked CallMeBot for WhatsApp — assert the GET URL is correct."""
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        return FakeResponse(b"Message queued.")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    r = connectors.send_whatsapp("hello world", phone="+919876543210",
                                 apikey="123456")
    assert r["ok"] is True
    assert seen["method"] == "GET"
    # callmebot.com base + phone-without-plus + url-encoded text + apikey
    assert seen["url"].startswith("https://api.callmebot.com/whatsapp.php?")
    assert "phone=919876543210" in seen["url"]
    assert "text=hello+world" in seen["url"] or "text=hello%20world" in seen["url"]
    assert "apikey=123456" in seen["url"]


def test_send_whatsapp_network_error_returns_ok_false(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("dns failure")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    r = connectors.send_whatsapp("hi", phone="1", apikey="k")
    assert r["ok"] is False and "URLError" in r["error"]


def test_send_via_connector_disabled_returns_off_without_network(monkeypatch):
    called = {"n": 0}

    def fake_urlopen(*a, **kw):
        called["n"] += 1
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    r = connectors.send_via_connector(
        "telegram", "hi",
        {"telegram": {"enabled": False, "bot_token": "x", "chat_id": "y"}})
    assert r["ok"] is False and "disabled" in r["error"]
    assert called["n"] == 0


def test_send_via_connector_unknown_name():
    r = connectors.send_via_connector("smoke-signal", "hi", {})
    assert r["ok"] is False and "unknown" in r["error"]


# --------------------------------------------------------------------------- #
# fleet.py config helpers (load/update under the lock)
# --------------------------------------------------------------------------- #
def test_load_connectors_returns_merged_block(cfg):
    block = fleet.load_connectors(cfg)
    assert set(block.keys()) == {"telegram", "whatsapp"}
    assert block["telegram"]["bot_token"] == "111:secretAAAA1234"


def test_update_connector_round_trip_persists(cfg):
    saved = fleet.update_connector(
        "telegram",
        {"enabled": True, "bot_token": "newbot:zzzz", "chat_id": "5"})
    assert saved == {"enabled": True, "bot_token": "newbot:zzzz", "chat_id": "5"}
    # round-trip from disk
    block = fleet.load_connectors(cfg)
    assert block["telegram"]["bot_token"] == "newbot:zzzz"
    # whatsapp is preserved (we only updated telegram)
    assert block["whatsapp"]["phone"] == "+919876543210"


def test_update_connector_partial_merge_preserves_existing(cfg):
    """A partial PUT (just the chat_id) must keep the existing bot_token."""
    fleet.update_connector("telegram", {"chat_id": "999"})
    block = fleet.load_connectors(cfg)
    assert block["telegram"]["chat_id"] == "999"
    assert block["telegram"]["bot_token"] == "111:secretAAAA1234"


def test_update_connector_rejects_enable_without_credentials(tmp_path, monkeypatch):
    cfg_path = tmp_path / "fleet.json"
    _write_cfg(cfg_path, {
        "projects": [],
        "agents": [{"name": "a", "role": "", "project_dir": str(tmp_path)}],
        "connectors": {"telegram": {"enabled": False, "bot_token": "",
                                    "chat_id": ""}},
    })
    monkeypatch.setattr(fleet, "CONFIG", cfg_path)
    monkeypatch.setattr(fleet, "AGENTS", fleet.load_config(cfg_path))
    with pytest.raises(ValueError):
        fleet.update_connector("telegram", {"enabled": True})


# --------------------------------------------------------------------------- #
# HTTP endpoints
# --------------------------------------------------------------------------- #
def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, json.loads(r.read() or b"{}")


def _request(url, method, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_http_get_connectors_masks_token(cfg):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        st, body = _get(base + "/api/connectors")
        assert st == 200
        assert "telegram" in body["connectors"]
        # secrets are masked: only the last 4 chars are revealed
        assert body["connectors"]["telegram"]["bot_token"] == "***1234"
        # chat_id is NOT a secret — comes back in cleartext
        assert body["connectors"]["telegram"]["chat_id"] == "987654321"
        assert body["connectors"]["whatsapp"]["apikey"] == "***9999"
        assert body["connectors"]["whatsapp"]["phone"] == "+919876543210"
        # configured flag lets the UI render "(not set)" vs the masked value
        assert body["connectors"]["telegram"]["bot_token_configured"] is True
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_http_put_connector_persists(cfg):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        st, body = _request(
            base + "/api/connectors/telegram", "PUT",
            {"enabled": True, "bot_token": "newtoken:ZZZZ", "chat_id": "42"})
        assert st == 200
        assert body["ok"] is True
        # response masks the token (server NEVER echoes plaintext)
        assert body["connector"]["bot_token"] == "***ZZZZ"
        # but it persisted in plaintext on disk
        block = fleet.load_connectors(cfg)
        assert block["telegram"]["bot_token"] == "newtoken:ZZZZ"
        assert block["telegram"]["chat_id"] == "42"
        assert block["telegram"]["enabled"] is True
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_http_put_connector_rejects_unknown_name(cfg):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        st, body = _request(base + "/api/connectors/discord", "PUT",
                            {"enabled": True})
        assert st == 400 and "unknown connector" in body["error"]
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_http_test_endpoint_invokes_send(cfg, monkeypatch):
    seen: dict = {}

    def fake_send(name, text, config):
        seen["name"] = name
        seen["text"] = text
        return {"ok": True, "error": None, "status": 200}

    monkeypatch.setattr(connectors, "send_via_connector", fake_send)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        st, body = _request(base + "/api/connectors/telegram/test", "POST", {})
        assert st == 200 and body["ok"] is True
        assert seen["name"] == "telegram"
        assert "Orchestrator connector test from" in seen["text"]
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_http_test_endpoint_surfaces_failure_as_200_ok_false(cfg, monkeypatch):
    monkeypatch.setattr(connectors, "send_via_connector",
                        lambda *a, **k: {"ok": False, "error": "boom"})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        st, body = _request(base + "/api/connectors/whatsapp/test", "POST", {})
        # we deliberately return 200 with ok=false so the UI shows the error
        # inline instead of treating it like a transport-layer 500.
        assert st == 200
        assert body["ok"] is False and body["error"] == "boom"
    finally:
        srv.shutdown()
        t.join(timeout=5)


# --------------------------------------------------------------------------- #
# _escalate_task fan-out
# --------------------------------------------------------------------------- #
def test_escalate_task_fans_out_to_every_enabled_connector(cfg, monkeypatch):
    seen: list[tuple[str, str]] = []

    def fake_send(name, text, config):
        seen.append((name, text))
        return {"ok": True, "error": None}

    monkeypatch.setattr(connectors, "send_via_connector", fake_send)
    monkeypatch.setattr(fleet, "send_message", lambda *a, **k: True)

    task = {"id": "t-0007", "attempts": 2,
            "last_error": "rate-limit exhausted"}
    fleet._escalate_task(task, "alice")

    sent_to = {name for name, _ in seen}
    assert sent_to == {"telegram", "whatsapp"}      # both enabled in the cfg
    # message carries the task id, attempt count, and last error
    for _, text in seen:
        assert "t-0007" in text
        assert "rate-limit exhausted" in text
        assert "2 retries" in text or "after 2" in text


def test_escalate_task_swallows_connector_errors(cfg, monkeypatch):
    """A connector that raises must NOT propagate — the escalation still
    completes (PM notified / attention flag flipped), and the failure is
    recorded on the task."""

    def fake_send(name, text, config):
        if name == "telegram":
            raise RuntimeError("telegram api blew up")
        return {"ok": True, "error": None}

    monkeypatch.setattr(connectors, "send_via_connector", fake_send)
    monkeypatch.setattr(fleet, "send_message", lambda *a, **k: True)

    task = {"id": "t-0008", "attempts": 3, "last_error": "x"}
    fleet._escalate_task(task, "alice")    # must not raise

    assert "connector_results" in task
    assert task["connector_results"]["telegram"]["ok"] is False
    assert task["connector_results"]["whatsapp"]["ok"] is True


def test_escalate_task_skips_disabled_connectors(tmp_path, monkeypatch):
    cfg_path = tmp_path / "fleet.json"
    pdir = tmp_path / "p"
    pdir.mkdir()
    _write_cfg(cfg_path, {
        "projects": [],
        "agents": [{"name": "alice", "role": "", "project_dir": str(pdir)}],
        "connectors": {
            "telegram": {"enabled": True, "bot_token": "t", "chat_id": "c"},
            "whatsapp": {"enabled": False, "phone": "", "apikey": ""},
        },
    })
    monkeypatch.setattr(fleet, "CONFIG", cfg_path)
    monkeypatch.setattr(fleet, "AGENTS", fleet.load_config(cfg_path))
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "NOTIFICATIONS_FILE",
                        tmp_path / "fleet_notifications.json")

    seen: list[str] = []
    monkeypatch.setattr(connectors, "send_via_connector",
                        lambda name, text, cfg: seen.append(name) or
                        {"ok": True, "error": None})
    monkeypatch.setattr(fleet, "send_message", lambda *a, **k: True)

    task = {"id": "t-0009", "attempts": 2, "last_error": "x"}
    fleet._escalate_task(task, "alice")
    assert seen == ["telegram"]   # whatsapp disabled, must not fire
