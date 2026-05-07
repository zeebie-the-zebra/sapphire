// settings-tabs/dashboard.js — Hero-style command center.
// Identity row: orb (mood ring) + Sapphire name + Store/Help + status pills.
// Action panels: System / Updates / Backups / Maintenance, each with a
// dropdown of actions. Below the hero: Plugin Spotlight + Token Metrics.
// Design source: tmp/dashboard-hero.html (Variant B, Dancing Script).
import * as ui from '../../ui.js';
import { listStorePlugins } from '../../shared/store-api.js';

let updateStatus = null;

function _esc(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

const MOOD_LABELS = {
    healthy: 'Online',
    busy:    'Working',
    warn:    'Issues',
    error:   'Error',
    idle:    'Idle',
};
// Status word color in Maintenance — splash of mood color, matched to the orb.
const MOOD_COLORS = {
    healthy: '#22c97a',
    busy:    '#4a9eff',
    warn:    '#f5a623',
    error:   '#ff4f4f',
    idle:    '#6e8e7d',
};

export default {
    id: 'dashboard',
    name: 'Dashboard',
    icon: '🏠',
    description: 'System status, updates, and controls',

    render(ctx) {
        const displayName = (() => {
            try { return localStorage.getItem('sapphireDisplayName') || 'Sapphire'; }
            catch (e) { return 'Sapphire'; }
        })();
        const version = window.__appVersion || '?';
        return `
            <div class="dash-root">
                <div class="dash-hero" id="dash-hero">
                    <div class="dash-npc-star" id="dash-npc-star"></div>
                    <div class="dash-hero-top">
                        <div class="dash-orb-block">
                            <div class="dash-orb" data-mood="healthy" id="dash-orb">
                                <div class="dash-orb-core"></div>
                            </div>
                        </div>

                        <div class="dash-hero-title">
                            <div class="dash-hero-name"
                                 id="dash-hero-name"
                                 contenteditable="plaintext-only"
                                 spellcheck="false"
                                 title="Click to rename">${_esc(displayName)}</div>
                            <div class="dash-hero-meta"><strong>v${_esc(version)}</strong> <span id="dash-branch"></span></div>
                        </div>

                        <div class="dash-hero-right">
                            <div class="dash-quick-links">
                                <button class="dash-hero-link" id="dash-open-store">\u{1F6CD}\u{FE0F} Store</button>
                                <button class="dash-hero-link" id="dash-open-help">\u{1F4D6} Help</button>
                            </div>
                            <div class="dash-component-status" id="dash-component-status">
                                ${['emb', 'tts', 'stt', 'ww'].map(k =>
                                    `<span class="dash-cs-pill" data-cs="${k}" title="${k.toUpperCase()}: loading"><span>${k}</span><span class="dash-cs-dot idle"></span></span>`
                                ).join('')}
                            </div>
                        </div>
                    </div>

                    <div class="dash-action-panels" id="dash-panels">
                        <span class="dim" style="font-size:11px;padding:8px">Loading widgets...</span>
                    </div>
                </div>

                <div class="dash-deps-card" id="dash-deps-card" style="display:none">
                    <h4 style="margin:0 0 8px;font-size:var(--font-sm);color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em">Missing Dependencies</h4>
                    <div id="dash-deps-list" style="font-size:var(--font-sm)">
                        <span class="text-muted">Checking...</span>
                    </div>
                </div>

                <div class="dash-content">
                    <div class="dash-card">
                        <div class="dash-card-header">
                            <h4>Token Metrics <span class="text-muted" style="font-size:var(--font-xs);font-weight:normal">(30 days)</span></h4>
                            <label class="metrics-toggle" id="metrics-toggle">
                                <input type="checkbox" id="metrics-enabled-cb">
                                <span class="toggle-track"></span>
                                <span class="toggle-label">Track</span>
                            </label>
                        </div>
                        <div id="dash-metrics" class="dash-metrics">
                            <span class="text-muted">Loading...</span>
                        </div>
                    </div>

                    <div class="dash-card dash-spotlight" id="dash-spotlight-card" style="display:none">
                        <h4>\u{1F6CD}\u{FE0F} Plugin Spotlight</h4>
                        <div class="dash-recommended-list" id="dash-recommended-list">
                            <span class="text-muted" style="font-size:var(--font-sm)">Loading...</span>
                        </div>
                        <a href="#store" class="dash-rec-see-all">See all in Store →</a>
                    </div>
                </div>
            </div>
        `;
    },

    attachListeners(ctx, el) {
        // ── Identity row ────────────────────────────────────────────
        _wireEditableName(el);
        _wireOrb(el);
        _startNpcStar(el);

        el.querySelector('#dash-open-store')?.addEventListener('click', () => {
            import('../../core/router.js').then(r => r.switchView('store'));
        });
        el.querySelector('#dash-open-help')?.addEventListener('click', () => {
            import('../../core/router.js').then(r => r.switchView('help'));
        });

        // ── Spotlight tile click → store deep-link (lower content row) ──
        el.querySelector('#dash-spotlight-card')?.addEventListener('click', e => {
            const tile = e.target.closest('.dash-rec-tile');
            if (tile) {
                window.location.hash = `#store/plugins/${encodeURIComponent(tile.dataset.slug)}`;
            }
        });

        // ── Mount panels via the widget registration system ─────────
        // The 5 built-ins each render through their own module. The host
        // owns the panel chrome (title + actions dropdown) and calls
        // module.render() to populate the body.
        mountPanels(el).catch(e => console.warn('mountPanels failed', e));

        // ── Initial mood paint (status word picks up its color when
        //    the Maintenance widget's #mnt-status is in the DOM) ──
        _setMood(el, el.querySelector('#dash-orb')?.getAttribute('data-mood') || 'healthy');

        // ── Hero-level data fetches ─────────────────────────────────
        // These keep the orb mood + component pills + lower content row
        // populated. Per-panel data fetches live inside each widget now.
        loadSystemInfo(el);
        checkForUpdate(el);
        loadComponentStatus(el);
        loadPluginSpotlight(el);
        loadMetrics(el);
        loadMissingDeps(el, ctx);
        checkLastUpdateResult();
    }
};


// =============================================================================
// HERO HELPERS
// =============================================================================

function _setMood(el, mood) {
    const orb = el.querySelector('#dash-orb');
    if (orb) orb.setAttribute('data-mood', mood);
    // The Maintenance widget renders #mnt-status async — may not be in
    // the DOM yet at first call. Subsequent mood-signal updates re-paint
    // it once the widget has mounted.
    const status = el.querySelector('#mnt-status');
    if (status) {
        const label = MOOD_LABELS[mood] || 'Online';
        const color = MOOD_COLORS[mood] || MOOD_COLORS.healthy;
        status.innerHTML = `status <strong style="color:${color}">${_esc(label)}</strong>`;
    }
}

function _wireEditableName(el) {
    const node = el.querySelector('#dash-hero-name');
    if (!node) return;
    node.addEventListener('blur', async e => {
        const v = (e.target.textContent || '').trim() || 'Sapphire';
        // Cap to reasonable length so a runaway paste can't deform the hero.
        const trimmed = v.slice(0, 64);
        e.target.textContent = trimmed;
        // localStorage cache for instant render on next load.
        try { localStorage.setItem('sapphireDisplayName', trimmed); } catch (e2) { /* ignore */ }
        // Persist to backend setting so it lives with the install, not the
        // browser. Falls back silently if the network call fails — the local
        // cache still carries the change.
        try {
            const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
            await fetch('/api/settings/batch', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
                body: JSON.stringify({ settings: { DASHBOARD_DISPLAY_NAME: trimmed }, persist: true }),
            });
        } catch { /* offline / network — local cache still has it */ }
    });
    node.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); }
        if (e.key === 'Escape') {
            try { e.target.textContent = localStorage.getItem('sapphireDisplayName') || 'Sapphire'; }
            catch (e2) { e.target.textContent = 'Sapphire'; }
            e.target.blur();
        }
    });
}

function _wireOrb(el) {
    const orb = el.querySelector('#dash-orb');
    if (!orb) return;
    orb.addEventListener('click', e => {
        const rect = orb.getBoundingClientRect();
        const cx = e.clientX - rect.left;
        const cy = e.clientY - rect.top;
        const ripple = document.createElement('div');
        ripple.className = 'dash-orb-ripple';
        ripple.style.left = cx + 'px';
        ripple.style.top  = cy + 'px';
        orb.appendChild(ripple);
        ripple.addEventListener('animationend', () => ripple.remove());
        orb.classList.add('inflated');
        setTimeout(() => orb.classList.remove('inflated'), 550);
    });
}

// NPC star — wanders, visits [data-attention] markers, hides in the orb,
// returns. State machine with linger phase + per-state fidget, driven by
// rAF (no React, plain DOM). Cleanup is best-effort: if the user leaves
// the dashboard tab the rAF keeps running until the node is gone — cheap.
function _startNpcStar(el) {
    const hero = el.querySelector('#dash-hero');
    const star = el.querySelector('#dash-npc-star');
    if (!hero || !star) return;

    let x = 80, y = 60, tx = 80, ty = 60;
    let state = 'wander';
    let lingering = false;
    let lingerUntil = 0;
    let anchorX = 80, anchorY = 60;
    let opacity = 1;

    const lingerDuration = (s) => {
        if (s === 'visit')  return 2000 + Math.random() * 1500;
        if (s === 'orbit')  return 2800 + Math.random() * 2400;
        if (s === 'wander') return 1300 + Math.random() * 1500;
        if (s === 'home')   return 1900 + Math.random() * 1700;
        return 1500;
    };
    const arrivalThreshold = (s) => (s === 'home' ? 22 : 4);

    const pickState = () => {
        const r = Math.random();
        if (r < 0.42)      state = 'wander';
        else if (r < 0.72) state = 'visit';
        else if (r < 0.88) state = 'orbit';
        else               state = 'home';
        lingering = false;

        const rect = hero.getBoundingClientRect();
        if (state === 'wander') {
            tx = 60 + Math.random() * (rect.width - 120);
            ty = 25 + Math.random() * (rect.height - 50);
        } else if (state === 'visit') {
            const targets = hero.querySelectorAll('[data-attention]');
            if (targets.length) {
                const t = targets[Math.floor(Math.random() * targets.length)].getBoundingClientRect();
                tx = t.left + t.width / 2 - rect.left;
                ty = t.top + t.height / 2 - rect.top;
            } else {
                state = 'wander';
                tx = Math.random() * rect.width;
                ty = Math.random() * rect.height;
            }
        } else if (state === 'orbit') {
            tx = x + (Math.random() - 0.5) * 90;
            ty = y + (Math.random() - 0.5) * 50;
        } else if (state === 'home') {
            const orbEl = hero.querySelector('.dash-orb');
            if (orbEl) {
                const o = orbEl.getBoundingClientRect();
                tx = o.left + o.width / 2 - rect.left;
                ty = o.top + o.height / 2 - rect.top;
            }
        }
    };

    pickState();

    const tick = () => {
        if (!star.isConnected) return; // node gone, stop the loop
        const now = performance.now();
        if (!lingering) {
            const ease = state === 'visit' ? 0.028 : 0.014;
            x += (tx - x) * ease;
            y += (ty - y) * ease;
            const dist = Math.hypot(tx - x, ty - y);
            if (dist < arrivalThreshold(state)) {
                lingering = true;
                lingerUntil = now + lingerDuration(state);
                anchorX = x; anchorY = y;
            }
        } else {
            const t = now / 1000;
            if (state === 'wander') {
                x += (Math.random() - 0.5) * 0.7;
                y += (Math.random() - 0.5) * 0.7;
                x += (anchorX - x) * 0.02;
                y += (anchorY - y) * 0.02;
            } else if (state === 'visit') {
                const phase = t * 1.6;
                x = anchorX + Math.cos(phase) * 5;
                y = anchorY + Math.sin(phase) * 5;
            } else if (state === 'orbit') {
                const phase = t * 0.55;
                x = anchorX + Math.cos(phase) * 14;
                y = anchorY + Math.sin(phase) * 8;
            }
            if (now > lingerUntil) pickState();
        }

        if (state === 'home' && lingering) {
            opacity = Math.max(0, opacity - 0.05);
        } else {
            opacity = Math.min(1, opacity + 0.02);
        }

        star.style.transform = `translate(${x}px, ${y}px)`;
        star.style.opacity = opacity;
        requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
}


// =============================================================================
// PANEL MOUNTING — built-in widgets through the registration system
// =============================================================================
//
// Each entry below is a built-in widget that ships with core. Stage 2 of
// the dashboard-widgets plan adds GET /api/dashboard/widgets to load this
// list from user/webui/dashboard.json instead of hardcoding here. For
// Stage 1, the goal is just to render the same panels via the new
// register-import-render flow without behavior change.

const STAGE1_PANELS = [
    { instance_id: 'sys',  plugin: 'core', widget_id: 'system',         render_url: '/core-widgets/system.js',         size: '1x1' },
    { instance_id: 'upd',  plugin: 'core', widget_id: 'updates',        render_url: '/core-widgets/updates.js',        size: '1x1' },
    { instance_id: 'bkp',  plugin: 'core', widget_id: 'backups',        render_url: '/core-widgets/backups.js',        size: '1x1' },
    { instance_id: 'mnt',  plugin: 'core', widget_id: 'maintenance',    render_url: '/core-widgets/maintenance.js',    size: '1x1' },
    { instance_id: 'spot', plugin: 'core', widget_id: 'mini-spotlight', render_url: '/core-widgets/mini-spotlight.js', size: '1x1' },
];

// Cleanup callbacks returned by each widget's render(). Run when the
// dashboard tab leaves or panels remount.
let _panelRegistry = [];

function _buildPanelChrome(panel) {
    const div = document.createElement('div');
    div.className = `dash-action-panel size-${panel.size}`;
    div.dataset.panel = panel.widget_id;
    div.dataset.instance = panel.instance_id;
    div.innerHTML = `
        <div class="dash-action-panel-title"></div>
        <div class="dash-action-panel-info"></div>
        <details class="dash-action-dropdown" name="dash-hero-actions">
            <summary><span>Actions</span><span class="chev">▾</span></summary>
            <div class="dash-action-dropdown-menu"></div>
        </details>
    `;
    return div;
}

async function mountPanels(el) {
    const container = el.querySelector('#dash-panels');
    if (!container) return;
    // Tear down any previously-mounted panels first.
    for (const p of _panelRegistry) {
        try { p.cleanup?.(); } catch (e) { console.warn('panel cleanup', p.instance_id, e); }
    }
    _panelRegistry = [];
    container.innerHTML = '';

    // Shared API surface passed to each widget via ctx.api. Plugin widgets
    // will receive the same shape so they only need to learn one contract.
    const api = {
        fetch: (url, init) => window.fetch(url, init),
        toast: (msg, kind) => ui.showToast(msg, kind),
        listStorePlugins,
        pollForRestart: () => setTimeout(() => pollForRestart(), 2000),
        navigateSettingsTab: (tab) => {
            const settingsView = el.closest('.settings-view') || el.closest('[data-view="settings"]');
            if (settingsView) {
                settingsView.dispatchEvent(new CustomEvent('settings-navigate', { detail: { tab }, bubbles: true }));
            }
        },
    };

    const v = (window.__appVersion || 'dev');

    for (const panel of STAGE1_PANELS) {
        const wrapper = _buildPanelChrome(panel);
        container.appendChild(wrapper);
        const bodyEl = wrapper.querySelector('.dash-action-panel-info');
        const titleEl = wrapper.querySelector('.dash-action-panel-title');
        const menu = wrapper.querySelector('.dash-action-dropdown-menu');

        const ctx = {
            plugin: panel.plugin,
            widget_id: panel.widget_id,
            instance_id: panel.instance_id,
            size: panel.size,
            settings: panel.settings || {},
            pluginWebPath: panel.plugin === 'core' ? '/core-widgets/' : `/plugin-web/${panel.plugin}/`,
            api,
        };

        try {
            // Cache-bust on each app version so widget code refreshes after upgrade.
            const module = await import(`${panel.render_url}?v=${encodeURIComponent(v)}`);
            const result = await module.render(bodyEl, ctx);
            titleEl.textContent = result?.title || panel.widget_id;

            // Build action buttons via DOM (avoid innerHTML for onClick wiring).
            (result?.actions || []).forEach(a => {
                const btn = document.createElement('button');
                if (a.kind) btn.className = a.kind;
                if (a.icon) {
                    const ic = document.createElement('span');
                    ic.className = 'action-icon';
                    ic.textContent = a.icon;
                    btn.appendChild(ic);
                    btn.appendChild(document.createTextNode(' '));
                }
                btn.appendChild(document.createTextNode(a.label || ''));
                if (typeof a.onClick === 'function') btn.addEventListener('click', a.onClick);
                menu.appendChild(btn);
            });

            _panelRegistry.push({ instance_id: panel.instance_id, cleanup: result?.cleanup });
        } catch (e) {
            console.warn(`[panel ${panel.plugin}.${panel.widget_id}] render failed`, e);
            titleEl.textContent = panel.widget_id;
            bodyEl.innerHTML = `<div class="dash-action-panel-info-line"><span class="dim">render failed: ${_esc(e?.message || String(e))}</span></div>`;
        }
    }

    // After Maintenance widget mounts (which creates #mnt-status), make
    // sure the status word picks up the current mood color.
    _setMood(el, el.querySelector('#dash-orb')?.getAttribute('data-mood') || 'healthy');
}


// =============================================================================
// DATA LOADERS
// =============================================================================

// Hero-level system-info fetch — used for mood derivation (disk %) and
// display name sync. Per-panel data fetches now live in widget render
// modules. There's a small duplicate-fetch cost (System and Maintenance
// widgets call this same endpoint) but in V2 we'll add a shared signals
// API so widgets contribute without re-querying.
async function loadSystemInfo(el) {
    const nameEl = el.querySelector('#dash-hero-name');
    try {
        const res = await fetch('/api/dashboard/system-info');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const d = await res.json();
        if (typeof d.disk_pct === 'number') {
            _moodSignals.diskPct = d.disk_pct;
            _refreshMood(el);
        }
        if (d.display_name && nameEl && nameEl.textContent.trim() !== d.display_name) {
            nameEl.textContent = d.display_name;
            try { localStorage.setItem('sapphireDisplayName', d.display_name); } catch (e2) { /* ignore */ }
        }
    } catch { /* widgets show their own errors; mood stays at last known */ }
}

// Component status cache — lets the mood derivation read the latest snapshot.
let _componentStatus = { emb: 'idle', tts: 'idle', stt: 'idle', ww: 'idle' };

async function loadComponentStatus(el) {
    try {
        const res = await fetch('/api/dashboard/component-status');
        if (!res.ok) throw new Error('component-status failed');
        const d = await res.json();
        _componentStatus = {
            emb: d.emb || 'idle',
            tts: d.tts || 'idle',
            stt: d.stt || 'idle',
            ww:  d.ww  || 'idle',
        };
        Object.entries(_componentStatus).forEach(([k, v]) => _setComponentDot(el, k, v));
    } catch {
        // Endpoint failed — paint all warn so the user sees something's off.
        ['emb', 'tts', 'stt', 'ww'].forEach(k => _setComponentDot(el, k, 'warn'));
        _componentStatus = { emb: 'warn', tts: 'warn', stt: 'warn', ww: 'warn' };
    }
    _refreshMood(el);
}

// Derive an aggregate mood from component status + update availability +
// disk usage. Called after each signal updates so the orb reflects the
// freshest picture. Idle components are benign — they just mean the
// subsystem is configured off, not broken.
let _moodSignals = {
    updateAvailable: false,
    pluginUpdatesCount: 0,
    diskPct: 0,
};
function _refreshMood(el) {
    const statuses = Object.values(_componentStatus);
    let mood = 'healthy';
    if (statuses.includes('error')) {
        mood = 'error';
    } else if (
        statuses.includes('warn') ||
        _moodSignals.updateAvailable ||
        _moodSignals.pluginUpdatesCount > 0 ||
        _moodSignals.diskPct > 92
    ) {
        mood = 'warn';
    }
    _setMood(el, mood);
}

function _setComponentDot(el, key, status) {
    const pill = el.querySelector(`.dash-cs-pill[data-cs="${key}"]`);
    if (!pill) return;
    const dot = pill.querySelector('.dash-cs-dot');
    if (dot) dot.className = `dash-cs-dot ${status}`;
    pill.title = `${key.toUpperCase()}: ${status}`;
}


// =============================================================================
// UPDATES
// =============================================================================

// Hero-level update check — for mood signals + branch label only. The
// Updates panel widget renders the "current/available · vX · Xh ago"
// status line itself.
async function checkForUpdate(el, retry = 0) {
    try {
        const res = await fetch('/api/system/update-check');
        if (!res.ok) throw new Error('Check failed');
        updateStatus = await res.json();
        if (!updateStatus.last_check && retry < 3) {
            setTimeout(() => checkForUpdate(el, retry + 1), 2000);
            return;
        }
        // Branch label appears in the meta line under Sapphire's name.
        const branchEl = el.querySelector('#dash-branch');
        if (branchEl && updateStatus.branch) {
            const tag = updateStatus.is_fork ? `${updateStatus.branch} · fork` : updateStatus.branch;
            branchEl.textContent = `· ${_esc(tag)}`;
        }
        if (updateStatus.available) {
            window.dispatchEvent(new CustomEvent('update-available', { detail: updateStatus }));
            _moodSignals.updateAvailable = true;
        } else {
            _moodSignals.updateAvailable = false;
        }
        _refreshMood(el);
    } catch { /* widget shows the error in its own line */ }
}

function pollForRestart() {
    let attempts = 0;
    const maxAttempts = 300;
    const poll = async () => {
        attempts++;
        try {
            const res = await fetch('/api/health');
            if (res.ok) { window.location.reload(); return; }
        } catch {}
        if (attempts < maxAttempts) setTimeout(poll, 1000);
    };
    poll();
}

async function checkLastUpdateResult() {
    try {
        const res = await fetch('/api/system/last-update-result');
        if (!res.ok) return;
        const data = await res.json();
        const r = data.result;
        if (!r) return;
        if (r.success) {
            ui.showToast(r.message || 'Update applied', 'success');
        } else {
            ui.showToast(`Update did NOT apply: ${r.message}`, 'error');
        }
    } catch {}
}


// =============================================================================
// PLUGIN SPOTLIGHT — community shoutouts from sapphireblue.dev (lower content row)
// =============================================================================

async function loadPluginSpotlight(el) {
    const card = el.querySelector('#dash-spotlight-card');
    if (!card) return;
    let data;
    try {
        data = await listStorePlugins({ featured: true, perPage: 5 });
    } catch (e) {
        card.style.display = 'none';
        return;
    }
    const items = (data && data.items) || [];
    if (!items.length || data.unreachable) {
        card.style.display = 'none';
        return;
    }

    // Mood signal — partial (only covers featured plugins). The Updates
    // widget surfaces the same number visually; this just feeds the orb.
    const updateCount = items.filter(i => i.installed_state === 'update_available').length;
    _moodSignals.pluginUpdatesCount = updateCount;
    _refreshMood(el);

    const list = card.querySelector('#dash-recommended-list');
    if (!list) return;
    list.innerHTML = items.map(item => {
        const author = item.author_url
            ? `<a href="${_esc(item.author_url)}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">${_esc(item.author || 'Unknown')}</a>`
            : _esc(item.author || 'Unknown');
        const installed = item.installed_state === 'current'
            ? '<span class="dash-rec-installed">installed</span>'
            : item.installed_state === 'update_available'
                ? '<span class="dash-rec-update">update available</span>'
                : '';
        return `
            <button class="dash-rec-tile" data-slug="${_esc(item.slug)}" title="Open ${_esc(item.name)} in the Store">
                <div class="dash-rec-name">${_esc(item.name)} ${installed}</div>
                <div class="dash-rec-author">by ${author}</div>
                <div class="dash-rec-desc">${_esc(item.description || '')}</div>
            </button>`;
    }).join('');
    card.style.display = '';
}


// =============================================================================
// MISSING DEPENDENCIES
// =============================================================================

async function loadMissingDeps(el, ctx) {
    const card = el.querySelector('#dash-deps-card');
    const list = el.querySelector('#dash-deps-list');
    if (!card || !list) return;

    try {
        const res = await fetch('/api/webui/plugins');
        if (!res.ok) return;
        const data = await res.json();
        const withDeps = (data.plugins || []).filter(p => p.missing_deps?.length);
        if (!withDeps.length) {
            card.style.display = 'none';
            return;
        }
        card.style.display = '';
        list.innerHTML = withDeps.map(p => `
            <div style="display:flex;align-items:center;gap:8px;padding:4px 0;">
                <span>${p.icon || '🔌'}</span>
                <span style="flex:1"><strong>${_esc(p.title || p.name)}</strong> needs: ${_esc(p.missing_deps.join(', '))}</span>
                <button class="btn btn-sm dash-deps-fix" data-plugin="${_esc(p.name)}"
                    style="font-size:0.75em;padding:2px 10px;background:rgba(255,165,0,0.2);border:1px solid rgba(255,165,0,0.4);color:#e0a030;cursor:pointer;border-radius:var(--radius-sm)">
                    Fix
                </button>
            </div>
        `).join('');
        list.querySelectorAll('.dash-deps-fix').forEach(btn => {
            btn.addEventListener('click', () => {
                const settingsView = el.closest('.settings-view') || el.closest('[data-view="settings"]');
                if (settingsView) {
                    settingsView.dispatchEvent(new CustomEvent('settings-navigate', { detail: { tab: 'plugins' }, bubbles: true }));
                }
            });
        });
    } catch { card.style.display = 'none'; }
}


// =============================================================================
// TOKEN METRICS
// =============================================================================

const fmt = n => {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
    return String(n);
};

async function loadMetrics(el) {
    const metricsEl = el.querySelector('#dash-metrics');
    const cb = el.querySelector('#metrics-enabled-cb');
    if (!metricsEl) return;

    try {
        const toggleRes = await fetch('/api/metrics/enabled');
        if (toggleRes.ok) {
            const { enabled } = await toggleRes.json();
            if (cb) cb.checked = enabled;
        }
    } catch {}

    if (cb) {
        cb.addEventListener('change', async () => {
            try {
                await fetch('/api/metrics/enabled', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: cb.checked })
                });
                loadMetricsData(metricsEl, cb.checked);
            } catch { cb.checked = !cb.checked; }
        });
    }
    loadMetricsData(metricsEl, cb?.checked !== false);
}

async function loadMetricsData(el, enabled) {
    if (!enabled) {
        el.innerHTML = '<span class="text-muted">Metrics tracking is off. Per-message stats still show in chat.</span>';
        return;
    }
    try {
        const [sumRes, brkRes, dailyRes] = await Promise.all([
            fetch('/api/metrics/summary?days=30'),
            fetch('/api/metrics/breakdown?days=30'),
            fetch('/api/metrics/daily?days=30')
        ]);
        if (!sumRes.ok || !brkRes.ok || !dailyRes.ok) throw new Error('Metrics fetch failed');
        const summary = await sumRes.json();
        const breakdown = await brkRes.json();
        const daily = await dailyRes.json();
        renderMetrics(el, summary, breakdown.models || [], daily.daily || []);
    } catch (e) {
        el.innerHTML = '<span class="text-muted">No metrics data yet — send some messages to start collecting</span>';
    }
}

function renderMetrics(el, s, models, daily) {
    if (!s.total_calls) {
        el.innerHTML = '<span class="text-muted">No data yet — metrics start recording from this version</span>';
        return;
    }
    const totalInput = (s.total_prompt || 0) + (s.total_cache_read || 0);
    const cacheRate = totalInput > 0 && s.total_cache_read > 0
        ? Math.round((s.total_cache_read / totalInput) * 100) : null;

    el.innerHTML = `
        <div class="metrics-stats">
            <div class="metric-item">
                <div class="metric-value">${fmt(s.total_calls)}</div>
                <div class="metric-label">LLM Calls</div>
            </div>
            <div class="metric-item">
                <div class="metric-value">${fmt(s.total_tokens)}</div>
                <div class="metric-label">Total Tokens</div>
            </div>
            <div class="metric-item">
                <div class="metric-value">${fmt(s.total_prompt)}</div>
                <div class="metric-label">Input</div>
            </div>
            <div class="metric-item">
                <div class="metric-value">${fmt(s.total_completion)}</div>
                <div class="metric-label">Output</div>
            </div>
            ${s.total_thinking > 0 ? `
            <div class="metric-item">
                <div class="metric-value">${fmt(s.total_thinking)}</div>
                <div class="metric-label">Thinking</div>
            </div>` : ''}
            ${cacheRate !== null ? `
            <div class="metric-item">
                <div class="metric-value">${cacheRate}%</div>
                <div class="metric-label">Cache Hit</div>
            </div>` : ''}
        </div>
        <div class="metrics-charts">
            <div class="metrics-chart-container">
                <div class="chart-title">Daily Usage</div>
                <div id="chart-daily" class="chart-area"></div>
            </div>
            <div class="metrics-chart-container">
                <div class="chart-title">Models</div>
                <div id="chart-models" class="chart-area"></div>
            </div>
        </div>
    `;
    renderDailyChart(el.querySelector('#chart-daily'), daily);
    renderModelChart(el.querySelector('#chart-models'), models);
}


// =============================================================================
// SVG CHARTS (unchanged from prior version)
// =============================================================================

function renderDailyChart(el, daily) {
    if (!el || daily.length < 2) {
        if (el) el.innerHTML = '<span class="text-muted" style="font-size:var(--font-xs)">Need 2+ days of data</span>';
        return;
    }
    const W = 540, H = 120, PAD_L = 40, PAD_R = 8, PAD_T = 8, PAD_B = 20;
    const chartW = W - PAD_L - PAD_R;
    const chartH = H - PAD_T - PAD_B;
    const maxTokens = Math.max(...daily.map(d => d.tokens)) || 1;
    const points = daily.map((d, i) => {
        const x = PAD_L + (i / (daily.length - 1)) * chartW;
        const y = PAD_T + chartH - (d.tokens / maxTokens) * chartH;
        return { x, y, ...d };
    });
    const polyline = points.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
    const areaPoints = `${PAD_L},${PAD_T + chartH} ${polyline} ${points[points.length - 1].x.toFixed(1)},${PAD_T + chartH}`;
    const yMid = fmt(Math.round(maxTokens / 2));
    const yMax = fmt(maxTokens);
    const firstDate = daily[0].date.slice(5);
    const lastDate = daily[daily.length - 1].date.slice(5);
    const dots = points.map(p =>
        `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3" class="chart-dot">
            <title>${p.date}: ${fmt(p.tokens)} tokens, ${p.calls} calls</title>
        </circle>`
    ).join('');
    el.innerHTML = `
        <svg viewBox="0 0 ${W} ${H}" class="chart-svg">
            <line x1="${PAD_L}" y1="${PAD_T}" x2="${PAD_L + chartW}" y2="${PAD_T}" class="chart-grid"/>
            <line x1="${PAD_L}" y1="${PAD_T + chartH / 2}" x2="${PAD_L + chartW}" y2="${PAD_T + chartH / 2}" class="chart-grid"/>
            <line x1="${PAD_L}" y1="${PAD_T + chartH}" x2="${PAD_L + chartW}" y2="${PAD_T + chartH}" class="chart-grid"/>
            <text x="${PAD_L - 4}" y="${PAD_T + 4}" class="chart-label" text-anchor="end">${yMax}</text>
            <text x="${PAD_L - 4}" y="${PAD_T + chartH / 2 + 3}" class="chart-label" text-anchor="end">${yMid}</text>
            <text x="${PAD_L - 4}" y="${PAD_T + chartH + 3}" class="chart-label" text-anchor="end">0</text>
            <text x="${PAD_L}" y="${H - 2}" class="chart-label">${firstDate}</text>
            <text x="${PAD_L + chartW}" y="${H - 2}" class="chart-label" text-anchor="end">${lastDate}</text>
            <polygon points="${areaPoints}" class="chart-area-fill"/>
            <polyline points="${polyline}" class="chart-line"/>
            ${dots}
        </svg>
    `;
}

function renderModelChart(el, models) {
    if (!el || !models.length) {
        if (el) el.innerHTML = '<span class="text-muted" style="font-size:var(--font-xs)">No model data yet</span>';
        return;
    }
    const top = models.slice(0, 5);
    const maxTotal = Math.max(...top.map(m => m.total)) || 1;
    const BAR_H = 18, GAP = 6, LABEL_W = 100, BAR_AREA = 370, PAD_R = 70;
    const W = LABEL_W + BAR_AREA + PAD_R;
    const H = top.length * (BAR_H + GAP) + GAP;
    const bars = top.map((m, i) => {
        const y = GAP + i * (BAR_H + GAP);
        const barW = Math.max(2, (m.total / maxTotal) * BAR_AREA);
        const label = m.model.length > 14 ? m.model.slice(0, 13) + '…' : m.model;
        const totalPrompt = (m.prompt || 0) + (m.cache_read || 0);
        const cacheInfo = m.cache_read > 0 && totalPrompt > 0
            ? ` · cache ${Math.round((m.cache_read / totalPrompt) * 100)}%` : '';
        return `
            <text x="${LABEL_W - 4}" y="${y + BAR_H / 2 + 4}" class="chart-label" text-anchor="end">${label}</text>
            <rect x="${LABEL_W}" y="${y}" width="${barW.toFixed(1)}" height="${BAR_H}" class="chart-bar" rx="2">
                <title>${m.model}: ${fmt(m.total)} tokens, ${m.calls} calls${cacheInfo}</title>
            </rect>
            <text x="${LABEL_W + barW + 4}" y="${y + BAR_H / 2 + 4}" class="chart-label">${fmt(m.total)}${cacheInfo}</text>
        `;
    }).join('');
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="chart-svg">${bars}</svg>`;
}
