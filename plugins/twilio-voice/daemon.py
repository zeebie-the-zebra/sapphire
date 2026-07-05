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
(proven 2026-07-02). Signaling is TLS by default (Stage 2, 2026-07-05 — one
client-initiated flow, router SIP-ALG irrelevant); UDP stays a per-account
fallback in the account editor. Media is μ-law RTP over UDP on both.
"""
import base64
import json
import logging
import threading
import time
import uuid

logger = logging.getLogger(__name__)

_SOURCE = "incoming_call"
_RECONCILE_SEC = 12
_SIP_PORT_BASE = 5062
_RTP_PORT_BASE = 10080
_OUTBOUND_TTL = 90              # secs an originated call may take to bridge back

_plugin_loader = None
_endpoints = {}                 # scope -> {"endpoint": SipEndpoint, "thread": Thread, "started": ts}
_backoff = {}                   # scope -> {"delay": secs, "until": ts} — failing registrations
_BACKOFF_FAST_DEATH_SEC = 60    # endpoint died this fast = registration failure, back off
_BACKOFF_MAX_SEC = 300          # cap: one retry burst per 5 min (polite to Twilio)
_reconcile_thread = None
_stop = threading.Event()
_lock = threading.Lock()
_outbound = {}                  # scope -> pending outbound call (dict)
_outbound_lock = threading.Lock()


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
    """Delete expired per-caller ephemeral chats (guarded in history). Chats
    with a LIVE call are excluded — a call longer than its TTL must never lose
    its chat mid-conversation (the TTL stamp is at call START; _call_ended
    re-stamps at hangup so the TTL means 'after their last call')."""
    try:
        from core.api_fastapi import get_system
        system = get_system()
        live = set((getattr(system, "_twilio_active_calls", None) or {}).keys())
        system.llm_chat.session_manager.reap_ephemeral_chats(time.time(), exclude=live)
    except Exception as e:
        logger.debug(f"[TWILIO] reap skipped: {e}")


def _reconcile_once():
    desired = _desired_accounts()
    with _lock:
        # Self-heal: an endpoint whose serve thread has died is not really running.
        # Drop it so the desired-set logic below re-registers it. A FAST death
        # (registration rejected — 403 etc.) backs off exponentially instead of
        # hammering Twilio every cycle; a long-lived endpoint that died (network
        # blip) restarts on the next cycle as before.
        for scope, info in list(_endpoints.items()):
            if info["thread"].is_alive():
                # Survived past the fast-death window = registration works; forget
                # any escalated delay so a future failure starts the ladder fresh.
                if scope in _backoff and time.time() - info.get("started", 0) >= _BACKOFF_FAST_DEATH_SEC:
                    _backoff.pop(scope, None)
                continue
            _endpoints.pop(scope, None)
            if time.time() - info.get("started", 0) < _BACKOFF_FAST_DEATH_SEC:
                delay = min(_backoff.get(scope, {}).get("delay", _RECONCILE_SEC) * 2,
                            _BACKOFF_MAX_SEC)
                _backoff[scope] = {"delay": delay, "until": time.time() + delay}
                logger.warning(f"[TWILIO] endpoint '{scope}' failed fast (registration?) "
                               f"— next retry in {delay}s")
            else:
                _backoff.pop(scope, None)
                logger.warning(f"[TWILIO] endpoint '{scope}' thread is dead — restarting")
        running = set(_endpoints.keys())
        for scope in running - desired:                     # tasks disabled / account removed
            logger.info(f"[TWILIO] deregistering '{scope}' (no enabled task)")
            _stop_endpoint(scope, _endpoints.pop(scope))
        # Toggling a rule off (or removing the account) resets its backoff — that's
        # the user's "I fixed the config, retry NOW" lever.
        for scope in list(_backoff):
            if scope not in desired:
                _backoff.pop(scope, None)
        # stable port slots by sorted account order (NAT mapping stability)
        from core.credentials_manager import credentials
        all_scopes = sorted(a["scope"] for a in credentials.list_twilio_accounts())
        for scope in desired - running:
            hold = _backoff.get(scope)
            if hold and time.time() < hold["until"]:
                continue
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
        accept_call=lambda caller, s=scope: _outbound_fresh(s) or _rule_for(s, caller) is not None,
        transport=acct.get("transport") or "tls",
    )
    t = threading.Thread(target=ep.serve_forever, daemon=True, name=f"twilio-sip-{scope}")
    t.start()
    _endpoints[scope] = {"endpoint": ep, "thread": t, "started": time.time()}
    logger.info(f"[TWILIO] registering account '{scope}' (SIP {_SIP_PORT_BASE + slot})")


def _stop_endpoint(scope, rec):
    try:
        rec["endpoint"].stop()
        if rec["thread"].is_alive():
            rec["thread"].join(timeout=4)
    except Exception as e:
        logger.warning(f"[TWILIO] error stopping '{scope}': {e}")


def _outbound_fresh(scope):
    """True if an outbound call is pending bridge-back on this account."""
    with _outbound_lock:
        ob = _outbound.get(scope)
        return bool(ob and time.time() < ob["deadline"])


def _outbound_take(scope, x_header=None):
    """Claim the pending outbound call for an arriving INVITE, or None.
    A matching X-Sapphire-Call token is definitive; a fresh pending slot
    without the header is accepted too (Twilio may strip URI params —
    the probe log on the first real call settles which world we're in)."""
    with _outbound_lock:
        ob = _outbound.get(scope)
        if not ob:
            return None
        if time.time() >= ob["deadline"]:
            _outbound.pop(scope, None)
            return None
        if x_header and x_header != ob["token"]:
            return None
        return _outbound.pop(scope)


def place_call(to_number, to_name, goal, origin_chat, ephemeral=True,
               prompt=None, max_minutes=10.0, scope="default", opening_line=None):
    """Originate an outbound call via Twilio REST; the bridged leg arrives at
    our registered SIP endpoint and runs the live conversation engine.
    Returns (ok, message-for-the-AI)."""
    import httpx
    from core.credentials_manager import credentials
    from core.api_fastapi import get_system

    acct = credentials.get_twilio_account(scope)
    if not (acct.get("account_sid") and acct.get("auth_token")):
        return False, ("This number has no REST credentials — add Account SID + "
                       "Auth Token in Settings > Plugins > Twilio Voice.")
    if not acct.get("number"):
        return False, ("This Twilio account has no phone number configured — the user "
                       "must fill the Phone Number field in Settings > Plugins > "
                       "Twilio Voice and click Save.")
    with _lock:
        rec = _endpoints.get(scope)
        if not rec or not rec["thread"].is_alive():
            return False, ("The number isn't registered right now — an enabled "
                           "Triggers > Realtime rule must keep it online to receive "
                           "the bridged call.")
    system = get_system()
    # Busy is per-LINE now: a live call on ANOTHER number doesn't block this one
    # (one call per SIP endpoint until the serve-loop refactor — Phase III).
    calls = getattr(system, "_twilio_active_calls", None) or {}
    if any(c.get("scope") == scope for c in calls.values()):
        return False, "A call is already in progress on this line."
    with _outbound_lock:
        ob = _outbound.get(scope)
        if ob and time.time() < ob["deadline"]:
            return False, "Another outbound call is already being placed on this line."

    chat = origin_chat
    if ephemeral:
        chat = _setup_outbound_chat(system, scope, to_number, prompt, origin_chat)
        if chat is None:
            return False, "Could not set up the call's side chat."

    token = uuid.uuid4().hex[:16]
    with _outbound_lock:
        _outbound[scope] = {"token": token, "scope": scope, "to": to_number,
                            "to_name": to_name, "goal": goal, "chat": chat,
                            "origin_chat": origin_chat, "ephemeral": bool(ephemeral),
                            "opening_line": opening_line or "",
                            "deadline": time.time() + _OUTBOUND_TTL}

    sip_uri = f"sip:{acct['sip_user']}@{acct['sip_domain']}?X-Sapphire-Call={token}"
    twiml = (f'<Response><Dial answerOnBridge="true" timeLimit="{int(max_minutes * 60)}">'
             f'<Sip>{sip_uri}</Sip></Dial></Response>')
    try:
        resp = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{acct['account_sid']}/Calls.json",
            data={"To": to_number, "From": acct["number"], "Twiml": twiml},
            auth=(acct["account_sid"], acct["auth_token"]), timeout=15)
        if resp.status_code >= 300:
            with _outbound_lock:
                _outbound.pop(scope, None)
            logger.error(f"[TWILIO] originate failed {resp.status_code}: {resp.text[:300]}")
            return False, f"Twilio rejected the call ({resp.status_code}). Check the number and REST credentials."
    except Exception as e:
        with _outbound_lock:
            _outbound.pop(scope, None)
        return False, f"Could not reach Twilio to place the call: {e}"
    logger.info(f"[TWILIO] originated call to {to_number} ({to_name}) — chat '{chat}', goal: {goal[:80]}")
    where = "this chat" if chat == origin_chat else f"chat '{chat}' (a summary will land here after)"
    return True, (f"Dialing {to_name} now. When they answer you'll be live on the "
                  f"call, which runs in {where}. Goal noted: {goal}")


def _setup_outbound_chat(system, scope, to_number, prompt, origin_chat):
    """Fresh per-call side chat, marked for the ephemeral reaper.

    Identity truth for outbound: SHE places the call, so the side chat INHERITS
    the origin chat's prompt + voice — she goes out as whoever she was when she
    dialed. The tool's `prompt` param overrides per call. Realtime rules (the
    inbound gates/personas) are never consulted for outbound."""
    digits = "".join(c for c in (to_number or "") if c.isalnum()) or "unknown"
    safe = f"_phoneout_{scope}_{digits}".lower()
    try:
        existing = {c.get("name") for c in system.llm_chat.list_chats()}
        if safe not in existing:
            system.llm_chat.create_chat(safe)
        else:
            system.llm_chat.session_manager.clear_named_chat_messages(safe)
        patch = {"ephemeral_source": "twilio", "ephemeral_last_call": time.time(),
                 "ephemeral_ttl_min": 30.0, "emoji": "\U0001F4DE",
                 "toolset": "none"}                     # outbound line: no tools v1
        try:
            origin = system.llm_chat.session_manager.read_chat_settings(origin_chat) or {}
        except Exception:
            origin = {}
        if prompt:
            patch["prompt"] = prompt                    # explicit per-call persona
        elif origin.get("prompt"):
            patch["prompt"] = origin["prompt"]          # inherit who she is
        if origin.get("tts_voice"):
            patch["tts_voice"] = origin["tts_voice"]    # and how she sounds
        for _k in ("llm_primary", "llm_model"):
            if origin.get(_k):
                patch[_k] = origin[_k]                  # and which brain (2026-07-04:
                                                        # unset fell to global default
                                                        # = claude = phone latency)
        system.llm_chat.session_manager.set_named_chat_settings(safe, patch)
        return safe
    except Exception as e:
        logger.warning(f"[TWILIO] outbound chat setup failed: {e}")
        return None


def _rule_for(scope, caller):
    """The Realtime rule handling this caller on this number, or None.
    Most-specific-wins: a rule whose caller filter matches beats the no-filter
    catch-all; a rule whose filter fails is excluded. None -> decline the call.
    Enables 'just me' + 'everyone else' rules side by side on one number."""
    if not _plugin_loader:
        return None
    return _plugin_loader.get_enabled_daemon_task(
        "incoming_call", scope, payload={"caller": caller, "account": scope})


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

    # Outbound bridge-back? The pending slot (+ optional X-Sapphire-Call token,
    # stashed on the session by the endpoint) claims this INVITE before any
    # inbound rule runs.
    ob = _outbound_take(scope, getattr(session, "x_header", None))
    if ob:
        logger.info(f"[TWILIO] outbound bridge to {ob['to']} answered "
                    f"(x_header={'yes' if getattr(session, 'x_header', None) else 'MISSING'}, "
                    f"invite From showed: {caller})")
        task = None
        chat, ephemeral = ob["chat"], ob["ephemeral"]
        caller = ob["to"]                       # display the callee, not the bridge id
        direction = "outbound"
    else:
        direction = "inbound"
        task = _rule_for(scope, caller)
        if task is None:
            # accept_call should have declined pre-answer; belt-and-suspenders.
            logger.warning(f"[TWILIO] no rule matches {caller} post-answer — hanging up")
            session.stop()
            _call_ended(scope, caller, "", False, "no_matching_rule", 0.0, 0)
            return
        chat, ephemeral = _resolve_chat(system, scope, caller, task)

    # One live call per chat: the per-chat hooks (phone context, hangup sentinel)
    # resolve their call by chat name — two calls sharing one chat would cross wires
    # (and their turns would interleave in one history). Ephemeral chats make this
    # rare; persistent chat_target rules on two numbers can collide — reject clean.
    # The record is REGISTERED BEFORE the session starts (under _lock, so two
    # endpoint threads can't both claim one chat); popped again on any failure.
    # `note` is the rule's optional Phone-context text for the ghost_inject hook;
    # `session` lets the <<HANG UP>> sentinel hook arm hangup-after-drain.
    sid = uuid.uuid4().hex[:12]
    _note = ((task or {}).get("trigger_config", {}).get("phone_note") or "").strip()
    with _lock:
        calls = getattr(system, "_twilio_active_calls", None)
        if calls is None:
            calls = system._twilio_active_calls = {}
        if chat in calls:
            busy = True
        else:
            busy = False
            calls[chat] = {"caller": caller, "chat": chat, "note": _note,
                           "session": session, "direction": direction,
                           "scope": scope, "session_id": sid,
                           "goal": (ob or {}).get("goal", "")}
    if busy:
        logger.warning(f"[TWILIO] chat '{chat}' already hosting a call — rejecting")
        session.stop()
        _call_ended(scope, caller, chat, ephemeral, "busy", 0.0, 0)
        return

    mgr = system.get_conversation_manager()
    from .twilio_source import TwilioConversationSource

    def ctor(driver, gate):
        src = TwilioConversationSource(driver, gate, session)
        src.start()
        return src

    src = mgr.start_external(ctor, chat_name=chat, source_label="phone", session_id=sid)
    if src is None:
        logger.warning("[TWILIO] no conversation slot free — rejecting call")
        calls.pop(chat, None)
        session.stop()
        _call_ended(scope, caller, chat, ephemeral, "busy", 0.0, 0)
        return

    # Inbound: the Realtime rule's greeting (fallback: the account's).
    # Outbound: the tool's opening_line — SHE speaks first, like a real caller;
    # blank means wait for the callee's hello.
    if direction == "outbound":
        greeting = (ob.get("opening_line") or "").strip()
    else:
        greeting = ((task or {}).get("trigger_config", {}).get("greeting") or acct.get("greeting") or "").strip()
    if greeting:
        try:
            _voice = ((task or {}).get("trigger_config", {}).get("tts_voice") or "").strip() or None
            if not _voice:
                # Outbound (and rule-less) greetings speak in the call chat's voice.
                try:
                    _voice = (system.llm_chat.session_manager.read_chat_settings(chat) or {}).get("tts_voice") or None
                except Exception:
                    _voice = None
            audio = system.tts.generate_audio_data(greeting, voice=_voice)
            if not audio and _voice:
                # Bad/missing voice must degrade to the default voice, not silence.
                logger.warning(f"[TWILIO] greeting voice '{_voice}' produced no audio — falling back to default voice")
                audio = system.tts.generate_audio_data(greeting)
            if audio:
                src.feed_chunk({"audio_b64": base64.b64encode(audio).decode()})
        except Exception as e:
            logger.warning(f"[TWILIO] greeting failed: {e}")
        # Record the greeting as her opening line in the call's chat, so she knows
        # what she said on pickup — it's in-context for the caller's first reply and
        # shows in the chat. Written to the target chat directly (not active).
        try:
            system.llm_chat.session_manager.append_messages_to_chat(
                chat, [{"role": "assistant", "content": greeting}])
        except Exception as e:
            logger.warning(f"[TWILIO] greeting history write failed: {e}")

    started = time.time()
    session.wait_ended()
    try:
        mgr.stop_external(sid)
    except Exception as e:
        logger.warning(f"[TWILIO] conversation stop error: {e}")
    calls.pop(chat, None)
    duration = round(time.time() - started, 1)
    if ob and ob["origin_chat"] != chat:
        # Report back to the chat that placed the call — WITH the transcript, so
        # the caller-Sapphire actually knows what was said (the side chat is
        # ephemeral and gets reaped; this copy is the durable record).
        try:
            lines = []
            for m in (system.llm_chat.session_manager.read_chat_messages(chat) or []):
                role, txt = m.get("role"), (m.get("content") or "").strip()
                if role in ("user", "assistant") and txt:
                    lines.append(f"{ob['to_name'] if role == 'user' else 'Me'}: {txt}")
            transcript = "\n".join(lines)
            if len(transcript) > 6000:
                transcript = "…(earlier trimmed)…\n" + transcript[-6000:]
            body = (f"\U0001F4DE My call to {ob['to_name']} just ended ({int(duration)}s). "
                    + (f"Transcript:\n{transcript}" if transcript
                       else "No words made it into the record."))
            system.llm_chat.session_manager.append_messages_to_chat(
                ob["origin_chat"], [{"role": "assistant", "content": body}])
        except Exception as e:
            logger.warning(f"[TWILIO] report-back to '{ob['origin_chat']}' failed: {e}")
    _call_ended(scope, caller, chat, ephemeral, "hangup", duration, 0,
                direction=direction,
                origin_chat=(ob or {}).get("origin_chat", ""),
                goal=(ob or {}).get("goal", ""))
    logger.info(f"[TWILIO] {direction} call with {caller} on '{scope}' ended (chat={chat})")


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
        else:
            # Reap-on-call-start: if the prior call was longer ago than the TTL, wipe
            # the history so this call starts fresh (she won't remember the old one).
            # No background timer, no active-chat deletion — just clear the messages.
            prior = system.llm_chat.session_manager.read_chat_settings(safe) or {}
            prior_last = float(prior.get("ephemeral_last_call", 0) or 0)
            if prior_last and (time.time() - prior_last) > ttl_min * 60:
                system.llm_chat.session_manager.clear_named_chat_messages(safe)
                logger.info(f"[TWILIO] ephemeral chat '{safe}' idle > {ttl_min:.0f}min — cleared for a fresh call")
        # Mark for the reaper + stamp last-call now + carry the task's behavior.
        patch = {"ephemeral_source": "twilio", "ephemeral_last_call": time.time(),
                 "ephemeral_ttl_min": ttl_min, "emoji": "\U0001F4DE"}   # 📞
        # Carry the rule's behavior onto the throwaway chat. PROMPT not persona —
        # a persona drags a toolset with it, and on an outside line capability must
        # stay explicit. Provider/model map to the chat's own keys so the caller's
        # chat actually has a working model (not the system default).
        if (task or {}).get("prompt"):
            patch["prompt"] = task["prompt"]
        if (task or {}).get("toolset"):
            patch["toolset"] = task["toolset"]
        if tc.get("tts_voice"):                     # rule's voice -> per-stream TTS
            patch["tts_voice"] = tc["tts_voice"]
        prov = (task or {}).get("provider")
        if prov and prov != "auto":
            patch["llm_primary"] = prov
        if (task or {}).get("model"):
            patch["llm_model"] = task["model"]
        # Memory/knowledge scopes — a caller with tools reaches an ISOLATED scope,
        # never the owner's default.
        for _k, _v in (task or {}).items():
            if _k.startswith("scope_") and _v:
                patch[_k] = _v
        system.llm_chat.session_manager.set_named_chat_settings(safe, patch)
    except Exception as e:
        logger.warning(f"[TWILIO] ephemeral chat setup failed ({e}); using 'default'")
        return "default", False
    return safe, True


def _call_ended(scope, caller, chat, ephemeral, reason, duration_sec, turns,
                direction="inbound", origin_chat="", goal=""):
    """Fire the call_ended EVENT (user tasks) + the twilio_call_ended HOOK (plugins)."""
    if ephemeral and chat:
        # Re-stamp so the TTL means what the trigger help text says — the chat
        # "survives N minutes after their LAST call" — not N minutes after call
        # START (a call longer than its TTL would be reaped as it ends).
        try:
            from core.api_fastapi import get_system
            get_system().llm_chat.session_manager.set_named_chat_settings(
                chat, {"ephemeral_last_call": time.time()})
        except Exception as e:
            logger.debug(f"[TWILIO] last-call re-stamp failed: {e}")
    from core.credentials_manager import credentials
    number = credentials.get_twilio_account(scope).get("number", "")
    # chat_target key = the call's chat, so a call_ended task with "chat from payload"
    # posts its summary into the chat the call lived in.
    _emit("call_ended", {"caller": caller, "account": scope, "number": number,
                         "reason": reason, "duration_sec": duration_sec,
                         "chat_target": chat, "ephemeral": ephemeral,
                         "direction": direction, "origin_chat": origin_chat,
                         "goal": goal})
    try:
        from core.hooks import hook_runner, HookEvent
        ev = HookEvent()
        ev.metadata = {
            "hook": "twilio_call_ended", "account": scope, "number": number,
            "caller": caller, "direction": direction, "chat": chat,
            "ephemeral": ephemeral, "ended_at": time.time(),
            "duration_sec": duration_sec, "end_reason": reason, "turns": turns,
            "origin_chat": origin_chat, "goal": goal,
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
