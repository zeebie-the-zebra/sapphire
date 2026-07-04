"""ghost_inject hook — tells Sapphire she's on a live phone call.

Per-turn, invisible-to-the-caller, never-persisted medium context delivered on the
ghost rail (see docs/GHOST_MESSAGES.md). This is ambient-state context ONLY — the
voice medium and the caller's number — it never reads or fingerprints the caller's
words, so it stays clear of the Vanta-shape anti-pattern the rail is gated against.

Gated tightly: fires only while a phone call is active (conversation_source ==
"phone") AND only for that call's own chat, so a concurrent web/cron turn during a
call never picks it up.
"""


def ghost_inject(event):
    system = event.metadata.get("system")
    if not system:
        return
    # Only during a live phone call.
    if getattr(system, "conversation_source", None) != "phone":
        return
    call = getattr(system, "_twilio_active_call", None)
    if not call:
        return
    # Only THIS call's chat — not a concurrent turn in another chat during the call.
    try:
        current = system.llm_chat.session_manager._effective_chat_name()
        if current != call.get("chat"):
            return
    except Exception:
        pass

    caller = call.get("caller") or "an unknown number"
    # The rule's custom Phone-context text (from the Realtime modal) wins; {caller}
    # is substituted. Blank -> the sensible default below.
    note = (call.get("note") or "").strip()
    if note:
        base = note.replace("{caller}", caller)
    else:
        base = (
            f"You're on a live phone call with {caller}. The user's messages are voice "
            "transcriptions and may contain small errors — infer intent, don't nitpick "
            "wording. Your reply is spoken aloud, so keep it brief and conversational: "
            "no markdown, lists, code blocks, or emoji."
        )
    # Always appended — even under a custom note. An outside line must never
    # leave her unable to hang up (see hooks/hangup_sentinel.py).
    event.ghost_text = base + (
        " To hang up: say your goodbye and put <<HANG UP>> at the end of that same "
        "reply. Writing the tag IS the action — the call ends right after your final "
        "words play (the tag itself is never heard). Don't write it unless you mean "
        "to hang up now; even quoting it triggers it."
    )
