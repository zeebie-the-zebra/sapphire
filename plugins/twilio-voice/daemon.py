"""Twilio voice daemon — registers to a Twilio SIP domain and runs Sapphire's
conversation engine on each inbound phone call.

No open ports: outbound SIP registration + keepalive holds the NAT pinhole
(proven 2026-07-02). On a call, a TwilioConversationSource bridges the RTP to the
same ConversationDriver/engine that powers local + browser conversation mode.
Milestone 1 (UDP transport, single account via plugin settings). SIP ALG on the
router must be OFF for UDP; TLS transport is a later milestone.
"""
import base64
import json
import logging
import threading

logger = logging.getLogger(__name__)

_endpoint = None
_thread = None
_plugin_loader = None
_lock = threading.Lock()


def start(plugin_loader, settings):
    global _endpoint, _thread, _plugin_loader
    with _lock:
        _plugin_loader = plugin_loader
        domain = (settings.get("sip_domain") or "").strip()
        user = (settings.get("sip_user") or "").strip()
        password = (settings.get("sip_pass") or "").strip()
        if not (domain and user and password):
            logger.info("[TWILIO] not configured (need sip_domain/user/pass) — daemon idle")
            return
        if _thread and _thread.is_alive():
            logger.warning("[TWILIO] daemon already running — skipping double-start")
            return

        from .sip_endpoint import SipEndpoint
        _endpoint = SipEndpoint(
            domain=domain, user=user, password=password, on_call=_on_call,
            sip_port=int(settings.get("sip_port", 5062)),
            rtp_port=int(settings.get("rtp_port", 10080)),
        )
        _endpoint._call_chat = (settings.get("call_chat") or "default").strip()
        _endpoint._greeting = (settings.get("greeting") or "").strip()

        _thread = threading.Thread(target=_endpoint.serve_forever, daemon=True, name="twilio-sip")
        _thread.start()
    logger.info(f"[TWILIO] daemon started (registering to {domain})")


def stop():
    global _endpoint, _thread
    with _lock:
        if _endpoint:
            _endpoint.stop()
        if _thread and _thread.is_alive():
            _thread.join(timeout=5)
        _endpoint = None
        _thread = None
    logger.info("[TWILIO] daemon stopped")


def _on_call(caller, session):
    """A call is up. Run the conversation engine on it until the call ends."""
    from core.api_fastapi import get_system
    ep = _endpoint
    chat = getattr(ep, "_call_chat", "default")
    _emit("incoming_call", {"caller": caller, "chat": chat})

    try:
        system = get_system()
    except Exception as e:
        logger.error(f"[TWILIO] system not available for call: {e}")
        session.stop()
        return

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
        _emit("call_ended", {"caller": caller, "reason": "busy"})
        return

    # Optional greeting so the caller isn't met with dead air (she otherwise waits
    # for the caller to speak first — engine starts IDLE).
    greeting = getattr(ep, "_greeting", "")
    if greeting:
        try:
            audio = system.tts.generate_audio_data(greeting)
            if audio:
                src.feed_chunk({"audio_b64": base64.b64encode(audio).decode()})
        except Exception as e:
            logger.warning(f"[TWILIO] greeting failed: {e}")

    session.wait_ended()                 # blocks until caller hangs up / call ends
    try:
        mgr.stop()
    except Exception as e:
        logger.warning(f"[TWILIO] conversation stop error: {e}")
    _emit("call_ended", {"caller": caller, "reason": "hangup"})
    logger.info(f"[TWILIO] call with {caller} ended")


def _emit(source, payload):
    try:
        if _plugin_loader:
            _plugin_loader.emit_daemon_event(source, json.dumps(payload))
    except Exception as e:
        logger.debug(f"[TWILIO] emit {source} failed: {e}")
