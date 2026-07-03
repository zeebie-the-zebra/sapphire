"""Twilio voice daemon — multi-account SIP, gated by Triggers > Daemon tasks.

Each Twilio number is an account (credentials_manager.twilio_accounts). The daemon
runs a reconcile loop: a number is REGISTERED only when it's (a) configured AND
(b) has an enabled `incoming_call` daemon task selecting it — the same
active-task gating Discord uses (`plugins/discord/daemon.py`). Toggling the task
in Triggers > Daemon registers/deregisters the number on the next reconcile, so
that IS the on/off switch. Each call runs the live conversation engine in the
account's configured chat (per-chat persona = call behavior; per-task prompt
overlay deferred). `call_ended` emits for post-call automation tasks.

No open ports: outbound SIP registration + keepalive holds the NAT pinhole
(proven 2026-07-02). UDP transport (router SIP-ALG must be off); TLS is later.
"""
import base64
import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

_SOURCE = "incoming_call"
_RECONCILE_SEC = 12
_SIP_PORT_BASE = 5062
_RTP_PORT_BASE = 10080

_plugin_loader = None
_endpoints = {}                 # scope -> {"endpoint": SipEndpoint, "thread": Thread}
_reconcile_thread = None
_stop = threading.Event()
_lock = threading.Lock()


def start(plugin_loader, settings):
    global _plugin_loader, _reconcile_thread
    with _lock:
        _plugin_loader = plugin_loader
        _migrate_legacy_settings(settings)
        _stop.clear()
        if _reconcile_thread and _reconcile_thread.is_alive():
            logger.warning("[TWILIO] daemon already running — skipping double-start")
            return
        _reconcile_thread = threading.Thread(target=_reconcile_loop, daemon=True,
                                             name="twilio-reconcile")
        _reconcile_thread.start()
    logger.info("[TWILIO] daemon started (reconcile loop; numbers register when a "
                "Triggers > Daemon task enables them)")


def stop():
    global _reconcile_thread
    _stop.set()
    with _lock:
        for scope, rec in list(_endpoints.items()):
            _stop_endpoint(scope, rec)
        _endpoints.clear()
    if _reconcile_thread and _reconcile_thread.is_alive():
        _reconcile_thread.join(timeout=6)
    _reconcile_thread = None
    logger.info("[TWILIO] daemon stopped")


def _migrate_legacy_settings(settings):
    """One-time: fold the old single-account plugin settings into a 'default'
    twilio account so the working setup survives the move to multi-account."""
    try:
        from core.credentials_manager import credentials
        if credentials.list_twilio_accounts():
            return                                          # already have accounts
        dom = (settings.get("sip_domain") or "").strip()
        usr = (settings.get("sip_user") or "").strip()
        pw = (settings.get("sip_pass") or "").strip()
        if dom and usr and pw:
            credentials.set_twilio_account(
                "default", sip_domain=dom, sip_user=usr, sip_pass=pw,
                chat=(settings.get("call_chat") or "default").strip(),
                greeting=(settings.get("greeting") or "").strip())
            logger.info("[TWILIO] migrated legacy single-account settings → account 'default'")
    except Exception as e:
        logger.warning(f"[TWILIO] legacy migration skipped: {e}")


def _desired_accounts():
    """Accounts that should be REGISTERED: configured AND gated by an enabled task."""
    from core.credentials_manager import credentials
    configured = {a["scope"] for a in credentials.list_twilio_accounts() if a.get("configured")}
    gated = _plugin_loader.active_daemon_accounts(_SOURCE) if _plugin_loader else set()
    return configured & gated


def _reconcile_loop():
    # let the system finish booting before first registration
    for _ in range(8):
        if _stop.is_set():
            return
        time.sleep(1)
    while not _stop.is_set():
        try:
            _reconcile_once()
            _reap_ephemeral()
        except Exception as e:
            logger.error(f"[TWILIO] reconcile error: {e}", exc_info=True)
        _stop.wait(_RECONCILE_SEC)


def _reap_ephemeral():
    """Delete expired per-caller ephemeral chats (triple-guarded in history)."""
    try:
        from core.api_fastapi import get_system
        get_system().llm_chat.session_manager.reap_ephemeral_chats(time.time())
    except Exception as e:
        logger.debug(f"[TWILIO] reap skipped: {e}")


def _reconcile_once():
    desired = _desired_accounts()
    with _lock:
        running = set(_endpoints.keys())
        for scope in running - desired:                     # tasks disabled / account removed
            logger.info(f"[TWILIO] deregistering '{scope}' (no enabled task)")
            _stop_endpoint(scope, _endpoints.pop(scope))
        # stable port slots by sorted account order (NAT mapping stability)
        from core.credentials_manager import credentials
        all_scopes = sorted(a["scope"] for a in credentials.list_twilio_accounts())
        for scope in desired - running:
            _start_endpoint(scope, all_scopes.index(scope) if scope in all_scopes else 0)


def _start_endpoint(scope, slot):
    from core.credentials_manager import credentials
    from .sip_endpoint import SipEndpoint
    acct = credentials.get_twilio_account(scope)
    if not (acct.get("sip_domain") and acct.get("sip_user") and acct.get("sip_pass")):
        return
    ep = SipEndpoint(
        domain=acct["sip_domain"], user=acct["sip_user"], password=acct["sip_pass"],
        on_call=lambda caller, session, s=scope: _on_call(s, caller, session),
        sip_port=_SIP_PORT_BASE + slot, rtp_port=_RTP_PORT_BASE + slot * 2,
    )
    t = threading.Thread(target=ep.serve_forever, daemon=True, name=f"twilio-sip-{scope}")
    t.start()
    _endpoints[scope] = {"endpoint": ep, "thread": t}
    logger.info(f"[TWILIO] registering account '{scope}' (SIP {_SIP_PORT_BASE + slot})")


def _stop_endpoint(scope, rec):
    try:
        rec["endpoint"].stop()
        if rec["thread"].is_alive():
            rec["thread"].join(timeout=4)
    except Exception as e:
        logger.warning(f"[TWILIO] error stopping '{scope}': {e}")


def _on_call(scope, caller, session):
    """A call is up on `scope`. The gate task carries the behavior: a named
    chat_target runs the call there (persistent); blank = a per-caller ephemeral
    chat. NOTE: we do NOT emit 'incoming_call' — it's a realtime GATE source, not a
    fire-a-task event. call_ended is the automation event + hook."""
    from core.api_fastapi import get_system
    from core.credentials_manager import credentials
    acct = credentials.get_twilio_account(scope)
    try:
        system = get_system()
    except Exception as e:
        logger.error(f"[TWILIO] system unavailable for call: {e}")
        session.stop()
        return

    task = _plugin_loader.get_enabled_daemon_task("incoming_call", scope) if _plugin_loader else None
    chat, ephemeral = _resolve_chat(system, scope, caller, task)

    mgr = system.get_conversation_manager()
    from .twilio_source import TwilioConversationSource

    def ctor(driver, gate):
        src = TwilioConversationSource(driver, gate, session)
        src.start()
        return src

    src = mgr.start_external(ctor, chat_name=chat, source_label="phone")
    if src is None:
        logger.warning("[TWILIO] conversation slot busy — rejecting call")
        session.stop()
        _call_ended(scope, caller, chat, ephemeral, "busy", 0.0, 0)
        return

    greeting = acct.get("greeting") or ""
    if greeting:
        try:
            audio = system.tts.generate_audio_data(greeting)
            if audio:
                src.feed_chunk({"audio_b64": base64.b64encode(audio).decode()})
        except Exception as e:
            logger.warning(f"[TWILIO] greeting failed: {e}")

    started = time.time()
    session.wait_ended()
    try:
        mgr.stop()
    except Exception as e:
        logger.warning(f"[TWILIO] conversation stop error: {e}")
    _call_ended(scope, caller, chat, ephemeral, "hangup",
                round(time.time() - started, 1), 0)
    logger.info(f"[TWILIO] call with {caller} on '{scope}' ended (chat={chat})")


def _resolve_chat(system, scope, caller, task):
    """Resolve the chat a call runs in, from the gate task's config.

    trigger_config.ephemeral checked → a per-caller ephemeral chat
    (`_phone_<account>_<callerdigits>`), created if new, marked for the reaper
    with the task's TTL + persona/toolset. Otherwise → the task's chat_target
    (persistent), or 'default' if none. Returns (chat_name, is_ephemeral)."""
    tc = (task or {}).get("trigger_config", {})
    if not bool(tc.get("ephemeral")):
        chat_target = ((task or {}).get("chat_target") or "").strip()
        return (chat_target or "default"), False

    ttl_min = float(tc.get("ephemeral_minutes", 10) or 10)
    digits = "".join(c for c in (caller or "") if c.isalnum()) or "unknown"
    name = f"_phone_{scope}_{digits}"
    safe = "".join(c for c in name if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_").lower()
    try:
        existing = {c.get("name") for c in system.llm_chat.list_chats()}
        if safe not in existing:
            system.llm_chat.create_chat(name)
            logger.info(f"[TWILIO] created ephemeral chat '{safe}' for caller {caller}")
        # Mark for the reaper + stamp last-call now + carry the task's behavior.
        patch = {"ephemeral_source": "twilio", "ephemeral_last_call": time.time(),
                 "ephemeral_ttl_min": ttl_min, "emoji": "\U0001F4DE"}   # 📞
        persona = (task or {}).get("persona")
        if persona:
            patch["persona"] = persona
            try:
                from core.personas import persona_manager
                p = persona_manager.get(persona)
                if p and p.get("settings"):
                    patch.update(p["settings"]); patch["persona"] = persona
            except Exception:
                pass
        toolset = (task or {}).get("toolset")
        if toolset:
            patch["toolset"] = toolset
        system.llm_chat.session_manager.set_named_chat_settings(safe, patch)
    except Exception as e:
        logger.warning(f"[TWILIO] ephemeral chat setup failed ({e}); using 'default'")
        return "default", False
    return safe, True


def _call_ended(scope, caller, chat, ephemeral, reason, duration_sec, turns):
    """Fire the call_ended EVENT (user tasks) + the twilio_call_ended HOOK (plugins)."""
    from core.credentials_manager import credentials
    number = credentials.get_twilio_account(scope).get("number", "")
    # chat_target key = the call's chat, so a call_ended task with "chat from payload"
    # posts its summary into the chat the call lived in.
    _emit("call_ended", {"caller": caller, "account": scope, "number": number,
                         "reason": reason, "duration_sec": duration_sec,
                         "chat_target": chat, "ephemeral": ephemeral})
    try:
        from core.hooks import hook_runner, HookEvent
        ev = HookEvent()
        ev.metadata = {
            "hook": "twilio_call_ended", "account": scope, "number": number,
            "caller": caller, "direction": "inbound", "chat": chat,
            "ephemeral": ephemeral, "ended_at": time.time(),
            "duration_sec": duration_sec, "end_reason": reason, "turns": turns,
        }
        hook_runner.fire("twilio_call_ended", ev)
    except Exception as e:
        logger.debug(f"[TWILIO] twilio_call_ended hook error: {e}")


def _emit(source, payload):
    try:
        if _plugin_loader:
            _plugin_loader.emit_daemon_event(source, json.dumps(payload))
    except Exception as e:
        logger.debug(f"[TWILIO] emit {source} failed: {e}")
