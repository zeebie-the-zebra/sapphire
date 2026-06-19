"""Local mic source for conversation mode — headphone tier (v3 Rollout 2b).

Input-only capture: reads 512-sample 16k frames, scores VAD, pushes (pcm, is_speech)
to the driver. Her TTS plays through the normal output path, so use HEADPHONES to
avoid her voice echoing into the mic (the open-speaker case needs DTLN — Rollout 2
full — which swaps THIS source for a duplex DTLN one; the driver/engine are unchanged).

This object is the `acquire_audio` session for the fail-safe handoff: `start()` opens
the stream (raising on failure -> handoff restores wakeword), `close()` stops cleanly.
"""
import logging
import threading

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


class LocalMicSource:
    def __init__(self, driver, gate, sample_rate=16000, blocksize=512, device=None):
        self.driver = driver
        self.gate = gate
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.device = device
        self._stream = None
        self._thread = None
        self._running = False

    def start(self):
        """Open the mic stream and start the capture->VAD->driver loop. Raises on
        failure so the handoff can restore wakeword."""
        self.gate.reset()
        self.driver.reset()
        self._stream = sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="int16",
            blocksize=self.blocksize, device=self.device,
        )
        self._stream.start()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="conv-mic")
        self._thread.start()
        logger.info("[CONV] local mic source started (headphone tier)")

    def _loop(self):
        while self._running:
            try:
                data, _ = self._stream.read(self.blocksize)
            except Exception as e:
                logger.error(f"[CONV] mic read failed: {e}")
                break
            chunk = np.asarray(data).reshape(-1).astype(np.int16)
            try:
                is_sp = self.gate.is_speech(chunk)
                self.driver.push_frame(chunk.tobytes(), is_sp)
            except Exception as e:
                logger.error(f"[CONV] frame processing failed: {e}")

    def close(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                logger.debug(f"[CONV] mic stream close error: {e}")
            self._stream = None
        logger.info("[CONV] local mic source closed")
