"""Conversation mode (v3) — continuous-listen, no-wakeword voice subsystem.

The engine here is the turn-state machine (pure logic). Audio capture + AEC + VAD
(the front-door) and the STT/LLM/TTS turn driver are injected, so the engine is
unit-testable and shared across the browser and local front-doors.
"""
from .engine import ConversationEngine, IDLE, USER_SPEAKING, RESPONDING

__all__ = ["ConversationEngine", "IDLE", "USER_SPEAKING", "RESPONDING"]
