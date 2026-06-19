"""Per-frame speech/silence gate for conversation mode (v3 Rollout 2b).

Wraps silero VAD (512-sample 16k chunks) into a simple `is_speech(chunk) -> bool`.
`score_fn` is injectable so the gate is unit-testable without loading the model.
"""
import logging

logger = logging.getLogger(__name__)


class SpeechGate:
    def __init__(self, sample_rate=16000, threshold=None, score_fn=None):
        self.sample_rate = sample_rate
        if threshold is None:
            try:
                import config as _cfg
                threshold = float(getattr(_cfg, "STT_VAD_SPEECH_THRESHOLD", 0.5))
            except Exception:
                threshold = 0.5
        self.threshold = threshold
        self._vad = None
        if score_fn is not None:
            self._score = score_fn
        else:
            from core.stt.silero_vad import SileroVAD
            self._vad = SileroVAD(sample_rate=16000)
            self._score = self._vad.score_chunk

    def is_speech(self, chunk_int16):
        """chunk_int16: 512-sample int16 frame @ 16k. Returns True if speech."""
        try:
            return float(self._score(chunk_int16)) >= self.threshold
        except Exception as e:
            logger.debug(f"[CONV] VAD score failed: {e}")
            return False

    def reset(self):
        if self._vad is not None:
            self._vad.reset()
