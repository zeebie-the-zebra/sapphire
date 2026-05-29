// settings-tabs/videos-tab.js — Links to the Video Guide view (doesn't embed,
// to avoid clobbering the view's module state — same pattern as help-tab.js).

export default {
    id: 'videos',
    name: 'Videos',
    icon: '🎬',  // 🎬
    description: 'Video guides + community channels',

    render() {
        return `<div style="padding:20px;text-align:center">
            <h3 style="margin:0 0 12px">🎬 Video Guide</h3>
            <p class="text-muted" style="margin:0 0 16px">The Crash Course playlist plus community member channels</p>
            <button class="btn-primary" id="settings-open-videos" style="padding:8px 24px">Open videos page</button>
        </div>`;
    },

    attachListeners(ctx, el) {
        el.querySelector('#settings-open-videos')?.addEventListener('click', () => {
            import('../../core/router.js').then(r => r.switchView('video-guide'));
        });
    }
};
