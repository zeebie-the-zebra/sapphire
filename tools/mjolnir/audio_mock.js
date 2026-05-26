// audio_mock.js — Mjolnir's Audio constructor mock + state observer.
//
// Loaded as a regular script BEFORE audio.js (which is an ES module, deferred).
// Replaces window.Audio with MjolnirAudio so audio.js's `new Audio(url)` calls
// hit our instrumented version. Tracks lifecycle events for invariant checks.
//
// Configuration is per-scenario, written via MjolnirObserver.reset({...}).
//
// Universal invariants checked at end-of-scenario:
//   1. At any point in time, at most one MjolnirAudio is in "playing" state.
//   2. Total chunks enqueued by audio.js == total play() calls observed
//      (after accounting for autoplay/abort failures).
//   3. play() events occur in monotonic index order.

(function () {
    'use strict';

    const MjolnirObserver = {
        // List of all MjolnirAudio instances created this scenario
        instances: [],
        // Chronological log of every interesting event
        events: [],
        // Mutable config — set by reset() per scenario
        config: {
            playLatencyMs: 10,
            audioDurationMs: 100,
            autoplayBlocked: false,         // first play() rejects with NotAllowedError
            playRejectsWith: null,           // every play() rejects with this error name
            onendedNeverFires: false,        // audio "plays" forever
            onerrorAfterMs: null,            // fire onerror after N ms instead of onended
        },

        log(event, data) {
            this.events.push({
                t: performance.now(),
                event,
                ...data,
            });
        },

        reset(config) {
            this.instances = [];
            this.events = [];
            if (config) {
                // Merge — preserve defaults for unset keys
                Object.assign(this.config, {
                    playLatencyMs: 10,
                    audioDurationMs: 100,
                    autoplayBlocked: false,
                    playRejectsWith: null,
                    onendedNeverFires: false,
                    onerrorAfterMs: null,
                }, config);
            }
            this.log('reset', {});
        },

        // ─── Invariant helpers ────────────────────────────────────────────

        /** Maximum number of MjolnirAudio elements simultaneously in "playing"
         * state. >1 means overlap (the 10× bug). */
        maxConcurrentPlaying() {
            let max = 0;
            let cur = 0;
            for (const e of this.events) {
                if (e.event === 'play.started')       { cur++; if (cur > max) max = cur; }
                else if (e.event === 'play.ended')    { cur--; }
                else if (e.event === 'paused.while_playing') { cur--; }
            }
            return max;
        },

        /** Indexes that successfully reached `play.started` state, in order. */
        playedIndexes() {
            return this.events
                .filter(e => e.event === 'play.started')
                .map(e => e.index);
        },

        /** Indexes that had play() called (regardless of outcome). */
        attemptedIndexes() {
            return this.events
                .filter(e => e.event === 'play.called')
                .map(e => e.index);
        },

        /** AbortError count from play() rejections. */
        abortErrorCount() {
            return this.events.filter(
                e => e.event === 'play.rejected' && e.errorName === 'AbortError'
            ).length;
        },

        /** Returns a full snapshot for cross-process consumption (JSON-safe). */
        snapshot() {
            return {
                events: this.events.slice(),
                maxConcurrentPlaying: this.maxConcurrentPlaying(),
                playedIndexes: this.playedIndexes(),
                attemptedIndexes: this.attemptedIndexes(),
                abortErrorCount: this.abortErrorCount(),
                instanceCount: this.instances.length,
            };
        },
    };

    window.MjolnirObserver = MjolnirObserver;

    // ─── MjolnirAudio — replaces window.Audio ─────────────────────────────

    class MjolnirAudio {
        constructor(url) {
            this._src = url || '';
            this._paused = true;
            this._playStarted = false;
            this._endedFired = false;
            this._volume = 1.0;
            this._instanceId = MjolnirObserver.instances.length;
            this._chunkIndex = null;  // set later if we can extract it
            this.onended = null;
            this.onerror = null;
            this._pendingReject = null;
            this._endedTimer = null;
            this._errorTimer = null;
            MjolnirObserver.instances.push(this);
            MjolnirObserver.log('audio.created', {
                id: this._instanceId,
                url: url ? '<blob>' : '',
            });
        }

        // ─── Property accessors ──────────────────────────────────────────
        get paused() { return this._paused; }
        set paused(v) { this._paused = !!v; }

        get src() { return this._src; }
        set src(v) {
            const prev = this._src;
            this._src = v;
            // Setting src to '' on a pending play() rejects it (per HTML spec)
            if (v === '' && this._pendingReject && !this._playStarted) {
                MjolnirObserver.log('audio.src_aborted_pending', {
                    id: this._instanceId,
                });
                const reject = this._pendingReject;
                this._pendingReject = null;
                reject(this._makeError('AbortError',
                    'The play() request was interrupted by a new load request.'));
            }
            // If we were actively playing and src is cleared, audio stops
            if (v === '' && this._playStarted && !this._paused) {
                this._paused = true;
                MjolnirObserver.log('paused.while_playing', {
                    id: this._instanceId,
                    via: 'src_cleared',
                });
                if (this._endedTimer) clearTimeout(this._endedTimer);
                if (this._errorTimer) clearTimeout(this._errorTimer);
            }
        }

        get volume() { return this._volume; }
        set volume(v) { this._volume = v; }

        // ─── Methods ─────────────────────────────────────────────────────
        play() {
            const self = this;
            const cfg = MjolnirObserver.config;
            MjolnirObserver.log('play.called', { id: this._instanceId });

            return new Promise((resolve, reject) => {
                self._pendingReject = reject;
                setTimeout(() => {
                    // Was the element disposed (src='') before play resolved?
                    if (!self._pendingReject) {
                        return;  // already rejected via src setter
                    }
                    self._pendingReject = null;

                    if (cfg.autoplayBlocked && self._instanceId === 0) {
                        MjolnirObserver.log('play.rejected', {
                            id: self._instanceId,
                            errorName: 'NotAllowedError',
                        });
                        reject(self._makeError('NotAllowedError',
                            'play() failed because the user did not interact with the document first. autoplay'));
                        return;
                    }
                    if (cfg.playRejectsWith) {
                        MjolnirObserver.log('play.rejected', {
                            id: self._instanceId,
                            errorName: cfg.playRejectsWith,
                        });
                        reject(self._makeError(cfg.playRejectsWith, 'mock rejection'));
                        return;
                    }

                    // play() succeeds — audio now playing
                    self._paused = false;
                    self._playStarted = true;
                    MjolnirObserver.log('play.started', {
                        id: self._instanceId,
                        index: self._chunkIndex,
                    });
                    resolve();

                    // Schedule end of playback
                    if (cfg.onerrorAfterMs != null) {
                        self._errorTimer = setTimeout(() => {
                            if (!self._paused && !self._endedFired) {
                                self._paused = true;
                                MjolnirObserver.log('error.fired', { id: self._instanceId });
                                // ALSO log play.ended so the concurrent-play
                                // counter decrements — onerror means this
                                // element is no longer playing audibly.
                                MjolnirObserver.log('play.ended', {
                                    id: self._instanceId, via: 'error'
                                });
                                if (self.onerror) self.onerror(new Event('error'));
                            }
                        }, cfg.onerrorAfterMs);
                    } else if (!cfg.onendedNeverFires) {
                        self._endedTimer = setTimeout(() => {
                            if (!self._paused && !self._endedFired) {
                                self._paused = true;
                                self._endedFired = true;
                                MjolnirObserver.log('play.ended', { id: self._instanceId });
                                if (self.onended) self.onended();
                            }
                        }, cfg.audioDurationMs);
                    }
                    // else: onendedNeverFires → audio "plays" forever
                }, cfg.playLatencyMs);
            });
        }

        pause() {
            if (this._pendingReject && !this._playStarted) {
                // Pause on a still-loading element rejects the pending play()
                MjolnirObserver.log('pause.aborted_pending', { id: this._instanceId });
                const reject = this._pendingReject;
                this._pendingReject = null;
                reject(this._makeError('AbortError',
                    'pause() called before play() resolved.'));
            }
            if (!this._paused) {
                this._paused = true;
                MjolnirObserver.log('paused.while_playing', {
                    id: this._instanceId,
                    via: 'pause',
                });
                if (this._endedTimer) clearTimeout(this._endedTimer);
                if (this._errorTimer) clearTimeout(this._errorTimer);
            }
        }

        load() {
            MjolnirObserver.log('load.called', { id: this._instanceId });
            // load() restarts media → rejects pending play()
            if (this._pendingReject && !this._playStarted) {
                const reject = this._pendingReject;
                this._pendingReject = null;
                reject(this._makeError('AbortError',
                    'The play() request was interrupted by a new load request.'));
            }
        }

        _makeError(name, msg) {
            const err = new DOMException(msg, name);
            return err;
        }
    }

    // Replace the global Audio constructor BEFORE audio.js loads.
    window.Audio = MjolnirAudio;
})();
