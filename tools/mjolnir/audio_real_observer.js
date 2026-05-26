// audio_real_observer.js — Lane B variant.
//
// Unlike audio_mock.js (Lane A — fully replaces Audio with synthetic state),
// this wraps the REAL window.Audio constructor with lifecycle observers.
// Browser plays real audio (real decoders, real autoplay policy, real CPU
// usage subject to CDP throttling). We just watch what happens.
//
// Same MjolnirObserver API as the mock so the runner code is reusable.

(function () {
    'use strict';

    const MjolnirObserver = {
        instances: [],
        events: [],
        config: {},  // unused in Lane B but kept for API parity

        log(event, data) {
            this.events.push({ t: performance.now(), event, ...data });
        },

        reset(_config) {
            this.instances = [];
            this.events = [];
            this.log('reset', {});
        },

        maxConcurrentPlaying() {
            let max = 0, cur = 0;
            for (const e of this.events) {
                if (e.event === 'play.started')       { cur++; if (cur > max) max = cur; }
                else if (e.event === 'play.ended')    { cur--; }
                else if (e.event === 'paused.while_playing') { cur--; }
            }
            return max;
        },
        playedIndexes() {
            return this.events.filter(e => e.event === 'play.started').map(e => e.index);
        },
        attemptedIndexes() {
            return this.events.filter(e => e.event === 'play.called').map(e => e.index);
        },
        abortErrorCount() {
            return this.events.filter(
                e => e.event === 'play.rejected' && e.errorName === 'AbortError'
            ).length;
        },
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

    // Wrap (not replace) the real Audio constructor.
    const _RealAudio = window.Audio;
    window.Audio = function WrappedAudio(url) {
        const audio = new _RealAudio(url);
        const id = MjolnirObserver.instances.length;
        audio._mjolnirId = id;
        audio._chunkIndex = null;  // stamped by runner after dispatch
        MjolnirObserver.instances.push(audio);
        MjolnirObserver.log('audio.created', { id });

        // Wrap play() to log .called and intercept the promise outcome
        const _origPlay = audio.play.bind(audio);
        audio.play = function () {
            MjolnirObserver.log('play.called', { id, index: audio._chunkIndex });
            const p = _origPlay();
            if (p && typeof p.then === 'function') {
                return p.then(
                    () => {
                        MjolnirObserver.log('play.started', { id, index: audio._chunkIndex });
                        return undefined;
                    },
                    (err) => {
                        MjolnirObserver.log('play.rejected', {
                            id, errorName: err.name || 'Unknown',
                            message: err.message || ''
                        });
                        throw err;
                    }
                );
            }
            return p;
        };

        // Wrap pause() to log when actively playing audio gets paused
        const _origPause = audio.pause.bind(audio);
        audio.pause = function () {
            const wasPlaying = !audio.paused && !audio.ended;
            const ret = _origPause();
            if (wasPlaying) {
                MjolnirObserver.log('paused.while_playing', { id, via: 'pause' });
            }
            return ret;
        };

        // Event listeners for natural ended/error
        audio.addEventListener('ended', () => {
            MjolnirObserver.log('play.ended', { id, index: audio._chunkIndex });
        });
        audio.addEventListener('error', (_e) => {
            MjolnirObserver.log('error.fired', { id, index: audio._chunkIndex });
            // If audio was actively playing, also log play.ended for the counter
            if (!audio.paused) {
                MjolnirObserver.log('play.ended', { id, index: audio._chunkIndex, via: 'error' });
            }
        });

        return audio;
    };
    // Preserve prototype chain in case anything checks instanceof
    window.Audio.prototype = _RealAudio.prototype;
})();
