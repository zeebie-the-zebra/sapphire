// core-widgets/maintenance.js — Maintenance panel (uptime + status word).
// The status line picks up its color from the orb's mood — handled by the
// host since mood derivation is a hero-level concern. Here we just render
// the lines and expose the "status" element for the host to update.

function _esc(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export async function render(container, ctx) {
    let aborted = false;

    async function refresh() {
        try {
            const res = await ctx.api.fetch('/api/dashboard/system-info');
            if (!res.ok) throw new Error('failed');
            const d = await res.json();
            if (aborted) return;
            container.innerHTML = `
                <div class="dash-action-panel-info-line">uptime <strong>${_esc(d.uptime_str)}</strong></div>
                <div class="dash-action-panel-info-line" id="mnt-status">status <strong>—</strong></div>
            `;
            // Hand the status node to the hero so it can paint mood color.
            ctx.api.bindMaintenanceStatus?.(container.querySelector('#mnt-status'));
        } catch {
            if (aborted) return;
            container.innerHTML = `
                <div class="dash-action-panel-info-line"><span class="dim">uptime: —</span></div>
                <div class="dash-action-panel-info-line"><span class="dim">status: —</span></div>
            `;
        }
    }

    refresh();
    // Uptime ticks up — refresh once a minute.
    const tick = setInterval(refresh, 60_000);

    return {
        title: 'Maintenance',
        actions: [
            {
                icon: '⌫', label: 'Clear JS cache',
                onClick: () => {
                    if ('caches' in window) {
                        caches.keys().then(names => names.forEach(n => caches.delete(n)));
                    }
                    ctx.api.toast('Cache cleared — reloading...', 'success');
                    setTimeout(() => window.location.reload(true), 500);
                },
            },
            {
                icon: '⟳', label: 'Reload static assets',
                onClick: () => window.location.reload(),
            },
        ],
        cleanup: () => {
            aborted = true;
            clearInterval(tick);
        },
    };
}
