"""External-message connectors used to escalate task failures off-platform.

WhatsApp provider: **CallMeBot** (https://www.callmebot.com/blog/free-api-whatsapp-messages/).
We picked CallMeBot over Twilio because it needs no SDK, no account billing,
and no signed request — a single HTTPS GET with phone + apikey + text is
enough, which keeps the orchestrator on stdlib only. Trade-off: it is a
hobbyist endpoint with no SLA; if reliable production delivery is needed,
swap in Twilio (whose ``twilio`` SDK is the obvious upgrade path).

All sender functions return the same shape::

    {"ok": bool, "error": str | None, "status": int | None, "response": str}

so :func:`send_via_connector` can dispatch without caring which provider
won the config. Errors never raise — the caller (``_escalate_task``) must be
able to fan out across connectors without one failure masking the original
task escalation.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_DEFAULT_TIMEOUT = 8.0   # seconds; long enough for slow mobile networks


def _result(ok: bool, *, error: str | None = None, status: int | None = None,
            response: str = "") -> dict:
    return {"ok": ok, "error": error, "status": status, "response": response}


def _http(method: str, url: str, *, data: bytes | None = None,
          headers: dict[str, str] | None = None,
          timeout: float = _DEFAULT_TIMEOUT) -> dict:
    """Minimal urllib wrapper that never raises — returns the result shape."""
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:    # noqa: S310
            body = r.read().decode("utf-8", errors="replace")
            return _result(True, status=r.status, response=body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return _result(False, error=f"HTTP {e.code}: {body[:200]}",
                       status=e.code, response=body)
    except urllib.error.URLError as e:
        return _result(False, error=f"URLError: {e.reason}")
    except (TimeoutError, OSError) as e:
        return _result(False, error=f"network error: {e}")


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def send_telegram(text: str, *, bot_token: str, chat_id: str,
                  timeout: float = _DEFAULT_TIMEOUT) -> dict:
    """POST ``text`` to the Telegram bot identified by ``bot_token``.

    Endpoint: ``https://api.telegram.org/bot<TOKEN>/sendMessage``.
    Body: JSON ``{"chat_id": ..., "text": ..., "disable_web_page_preview": true}``.
    """
    if not bot_token or not chat_id:
        return _result(False, error="telegram: bot_token and chat_id required")
    if not text:
        return _result(False, error="telegram: empty text")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    return _http("POST", url, data=payload, headers=headers, timeout=timeout)


# --------------------------------------------------------------------------- #
# WhatsApp (CallMeBot)
# --------------------------------------------------------------------------- #
def send_whatsapp(text: str, *, phone: str, apikey: str,
                  timeout: float = _DEFAULT_TIMEOUT) -> dict:
    """GET ``https://api.callmebot.com/whatsapp.php?phone=...&text=...&apikey=...``.

    CallMeBot expects ``phone`` in international format **without** the leading
    ``+`` and ``text`` URL-encoded. ``apikey`` is issued once via their
    Telegram sign-up flow.
    """
    if not phone or not apikey:
        return _result(False, error="whatsapp: phone and apikey required")
    if not text:
        return _result(False, error="whatsapp: empty text")
    phone = phone.lstrip("+").strip()
    qs = urllib.parse.urlencode({"phone": phone, "text": text, "apikey": apikey})
    url = f"https://api.callmebot.com/whatsapp.php?{qs}"
    return _http("GET", url, timeout=timeout)


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #
SUPPORTED = ("telegram", "whatsapp")


def send_via_connector(name: str, text: str, config: dict[str, Any]) -> dict:
    """Look up the named connector in ``config`` and send ``text`` through it.

    ``config`` is the ``connectors`` block from ``fleet.json`` — see
    ``CONNECTOR_DEFAULTS`` for the shape. Returns the standard result dict.
    A disabled connector returns ``{"ok": False, "error": "<name> disabled"}``
    without making any network calls so the caller can treat "off" and "broken"
    distinctly.
    """
    if name not in SUPPORTED:
        return _result(False, error=f"unknown connector {name!r}")
    cfg = (config or {}).get(name) or {}
    if not cfg.get("enabled"):
        return _result(False, error=f"{name} disabled")
    if name == "telegram":
        return send_telegram(text,
                             bot_token=cfg.get("bot_token", ""),
                             chat_id=cfg.get("chat_id", ""))
    if name == "whatsapp":
        return send_whatsapp(text,
                             phone=cfg.get("phone", ""),
                             apikey=cfg.get("apikey", ""))
    return _result(False, error=f"unhandled connector {name!r}")


# --------------------------------------------------------------------------- #
# Config shape
# --------------------------------------------------------------------------- #
CONNECTOR_DEFAULTS: dict[str, dict] = {
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    "whatsapp": {"enabled": False, "phone": "", "apikey": ""},
}

# Which fields of each connector config are secret and must be masked when the
# block is returned to the dashboard. Only the last 4 characters are revealed.
SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "telegram": ("bot_token",),
    "whatsapp": ("apikey",),
}


def mask_secret(value: str) -> str:
    """Return ``"***1234"`` for a token whose last four chars are ``1234``.

    Empty strings stay empty so the dashboard can show "(not set)". Values
    shorter than 4 chars are fully masked.
    """
    s = str(value or "")
    if not s:
        return ""
    if len(s) <= 4:
        return "***"
    return "***" + s[-4:]


def mask_connectors_block(block: dict) -> dict:
    """Return a deep-ish copy of ``block`` with every SECRET_FIELDS value masked.

    The dashboard renders this; the unmasked values never leave the server.
    Unknown connector names are passed through unchanged (so future additions
    don't silently leak secrets — they just stay visible until added here).
    """
    out: dict = {}
    for name, cfg in (block or {}).items():
        cfg = dict(cfg or {})
        for field in SECRET_FIELDS.get(name, ()):
            if field in cfg:
                cfg[field] = mask_secret(cfg[field])
        out[name] = cfg
    return out


def merge_with_defaults(block: dict | None) -> dict:
    """Backfill missing connectors / fields from CONNECTOR_DEFAULTS.

    Used at read time so the dashboard always renders both cards even on a
    fresh ``fleet.json`` that hasn't yet had a ``connectors`` block.
    """
    out: dict = {}
    src = block or {}
    for name, defaults in CONNECTOR_DEFAULTS.items():
        cfg = dict(defaults)
        cfg.update(src.get(name) or {})
        out[name] = cfg
    return out


def validate_connector(name: str, cfg: dict) -> dict:
    """Return a normalized ``cfg`` for ``name``. Raise ``ValueError`` on error.

    Keeps only known fields, coerces ``enabled`` to bool, strings everything
    else. A connector can be saved with empty credentials (enabled=False)
    so the user can park half-typed config without losing it.
    """
    if name not in SUPPORTED:
        raise ValueError(f"unknown connector {name!r}")
    base = dict(CONNECTOR_DEFAULTS[name])
    for k in base:
        if k in cfg:
            base[k] = bool(cfg[k]) if k == "enabled" else str(cfg[k] or "")
    if base["enabled"]:
        # require credentials before turning a connector on, so a misconfigured
        # toggle can't silently no-op at escalation time.
        if name == "telegram" and not (base["bot_token"] and base["chat_id"]):
            raise ValueError("telegram: bot_token and chat_id required to enable")
        if name == "whatsapp" and not (base["phone"] and base["apikey"]):
            raise ValueError("whatsapp: phone and apikey required to enable")
    return base
