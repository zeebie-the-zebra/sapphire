// views/video-guide.js — in-app multi-channel community video viewer.
//
// Tabs: "Crash Course" (the curated playlist — the meat) + one tab per
// channel (mini-YouTube header + latest feed). Plays in-app via the
// youtube-nocookie player component. Watched-state in localStorage. ↻ Refresh
// forces a backend re-fetch. Deep-linkable: window._viewSelect = '<videoId>'
// or hash #video-guide/<videoId> opens straight into that video.

import { mountPlayer, clearPlayer } from '../features/video-player.js';

let container = null;
let data = null;             // { channels: [...] }
let activeTab = 'course';    // 'course' | <channel.key>
const WATCHED_KEY = 'vg_watched';
let watched = loadWatched();

function loadWatched() {
    try { return new Set(JSON.parse(localStorage.getItem(WATCHED_KEY) || '[]')); }
    catch { return new Set(); }
}
function saveWatched() {
    try { localStorage.setItem(WATCHED_KEY, JSON.stringify([...watched])); } catch {}
}

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
}
const thumb = (id) => `https://i.ytimg.com/vi/${id}/mqdefault.jpg`;
const PLAYLIST_URL = 'https://www.youtube.com/playlist?list=PL3x22_N-oxJEdAHy_GsokrMW9UzB13oTF';
const primary = () => data?.channels?.find(c => c.kind === 'primary');

async function fetchVideos(refresh = false) {
    try {
        const r = await fetch(`/api/videos${refresh ? '?refresh=1' : ''}`, { credentials: 'same-origin' });
        if (!r.ok) return { channels: [] };
        return await r.json();
    } catch { return { channels: [] }; }
}

/* ── Rendering ─────────────────────────────────────────────────────── */

function renderTabs() {
    const tabs = [];
    if (primary()?.course?.length) tabs.push({ key: 'course', label: '🎓 Crash Course' });
    for (const ch of data?.channels || []) tabs.push({ key: ch.key, label: ch.name });
    return `<div class="vg-tabs">${tabs.map(t =>
        `<button class="vg-tab${activeTab === t.key ? ' active' : ''}" data-tab="${esc(t.key)}">${esc(t.label)}</button>`
    ).join('')}</div>`;
}

function cardHTML(v, { showDur = false, num = null } = {}) {
    const w = watched.has(v.id) ? ' watched' : '';
    const badge = showDur && v.dur ? `<span class="vg-badge">${esc(v.dur)}</span>` : '';
    const idx = num != null ? `<span class="vg-num">${num}</span>` : '';
    return `<div class="vg-card${w}" data-vid="${esc(v.id)}" data-title="${esc(v.title)}" role="button" tabindex="0">
        <div class="vg-thumb">${idx}<img loading="lazy" referrerpolicy="no-referrer" src="${thumb(v.id)}" alt="">${badge}<span class="vg-playicon">▶</span></div>
        <div class="vg-card-title">${esc(v.title)}</div>
    </div>`;
}

function renderContent() {
    if (!data) return '<p class="vg-empty">Loading…</p>';
    if ((data.unreachable || !data.channels?.length)) {
        return '<p class="vg-empty">Couldn’t reach YouTube right now. Try ↻ Refresh in a moment.</p>';
    }
    if (activeTab === 'course') {
        const list = primary()?.course || [];
        if (!list.length) return '<p class="vg-empty">No course videos.</p>';
        const intro = `<div class="vg-intro">
            <p>New to Sapphire? This <strong>Crash Course</strong> walks through the whole system end to end — install, prompts, tools, memory, personas, tasks, plugins and more. Beginner-friendly, ~4.5 hours total. Watch in order, or jump to the part you need.</p>
            <a class="vg-chan-link" href="${PLAYLIST_URL}" target="_blank" rel="noopener">▶ Open the full playlist on YouTube ↗</a>
        </div>`;
        return intro + `<div class="vg-grid">${list.map((v, i) => cardHTML(v, { showDur: true, num: i + 1 })).join('')}</div>`;
    }
    const ch = data.channels.find(c => c.key === activeTab);
    if (!ch) return '<p class="vg-empty">Channel not found.</p>';
    const header = `<div class="vg-chan-head">
        ${ch.avatar ? `<img class="vg-avatar" src="${esc(ch.avatar)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ''}
        <div class="vg-chan-meta">
            <div class="vg-chan-name">${esc(ch.name)}</div>
            ${ch.desc ? `<div class="vg-chan-desc">${esc(ch.desc.slice(0, 200))}${ch.desc.length > 200 ? '…' : ''}</div>` : ''}
            <a class="vg-chan-link" href="${esc(ch.url)}" target="_blank" rel="noopener">Visit channel ↗</a>
        </div>
    </div>`;
    const latest = ch.latest || [];
    const grid = latest.length
        ? `<div class="vg-grid">${latest.map(v => cardHTML(v, { showDur: true })).join('')}</div>`
        : '<p class="vg-empty">No recent videos.</p>';
    return header + grid;
}

function render() {
    const root = container?.querySelector('.vg-root');
    if (!root) return;
    root.innerHTML = `
        <div class="vg-bar">
            <h2>Videos</h2>
            <button class="vg-refresh" id="vg-refresh" title="Re-fetch from YouTube">↻ Refresh</button>
        </div>
        ${renderTabs()}
        <div class="vg-content">${renderContent()}</div>`;
}

/* ── Player modal ──────────────────────────────────────────────────── */

function openPlayer(id, title) {
    if (!id) return;
    if (!watched.has(id)) { watched.add(id); saveWatched(); }
    const modal = container.querySelector('.vg-modal');
    modal.querySelector('.vg-modal-title').textContent = title || '';
    mountPlayer(modal.querySelector('.vg-modal-player'), id);
    modal.classList.add('open');
}

function closePlayer() {
    const modal = container?.querySelector('.vg-modal');
    if (!modal) return;
    modal.classList.remove('open');
    clearPlayer(modal.querySelector('.vg-modal-player'));
    render();  // refresh watched-dim
}

async function doRefresh() {
    const btn = container.querySelector('#vg-refresh');
    if (btn) { btn.disabled = true; btn.textContent = '↻ …'; }
    data = await fetchVideos(true);
    render();
}

/* ── Styles (injected once) ────────────────────────────────────────── */

function injectStyles() {
    if (document.getElementById('vg-styles')) return;
    const s = document.createElement('style');
    s.id = 'vg-styles';
    s.textContent = `
    .vg-root{padding:16px 24px;height:100%;flex:1;min-height:0;overflow-y:auto}
    .vg-bar{display:flex;align-items:center;justify-content:space-between;gap:12px}
    .vg-bar h2{margin:0}
    .vg-refresh{background:var(--bg-secondary,#222);color:var(--text,#eee);border:1px solid var(--border,#333);border-radius:6px;padding:6px 12px;cursor:pointer}
    .vg-refresh:hover{border-color:var(--trim,var(--accent,#6cf))}
    .vg-tabs{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0 16px;border-bottom:1px solid var(--border,#333);padding-bottom:8px}
    .vg-tab{background:transparent;color:var(--text-muted,#aaa);border:1px solid transparent;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:.95em}
    .vg-tab:hover{color:var(--text,#eee)}
    .vg-tab.active{color:var(--text,#fff);background:var(--bg-secondary,#222);border-color:var(--border,#333)}
    .vg-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}
    .vg-card{cursor:pointer;border-radius:8px;overflow:hidden;background:var(--bg-secondary,#1b1b1b);border:1px solid var(--border,#2a2a2a);transition:border-color .12s,transform .12s}
    .vg-card:hover{border-color:var(--trim,var(--accent,#6cf));transform:translateY(-2px)}
    .vg-card:focus-visible{outline:2px solid var(--accent,#6cf)}
    .vg-thumb{position:relative;aspect-ratio:16/9;background:#000}
    .vg-thumb img{width:100%;height:100%;object-fit:cover;display:block}
    .vg-badge{position:absolute;right:6px;bottom:6px;background:rgba(0,0,0,.82);color:#fff;font-size:.78em;padding:1px 6px;border-radius:4px}
    .vg-num{position:absolute;left:6px;top:6px;z-index:1;background:rgba(0,0,0,.82);color:#fff;font-size:.78em;padding:1px 7px;border-radius:10px}
    .vg-playicon{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:2.2em;color:#fff;opacity:0;transition:opacity .12s;text-shadow:0 2px 8px rgba(0,0,0,.6)}
    .vg-card:hover .vg-playicon{opacity:.92}
    .vg-card-title{padding:9px 10px;font-size:.9em;line-height:1.3;color:var(--text,#eee);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
    .vg-card.watched .vg-thumb img{opacity:.45}
    .vg-card.watched .vg-card-title::after{content:" ✓";color:var(--success,#5c9)}
    .vg-chan-head{display:flex;align-items:center;gap:14px;margin:4px 0 18px}
    .vg-avatar{width:64px;height:64px;border-radius:50%;object-fit:cover;border:1px solid var(--border,#333)}
    .vg-chan-name{font-size:1.25em;font-weight:600;color:var(--text,#fff)}
    .vg-chan-link{color:var(--accent,#6cf);text-decoration:none;font-size:.9em}
    .vg-chan-link:hover{text-decoration:underline}
    .vg-empty{color:var(--text-muted,#999);padding:30px 0;text-align:center}
    .vg-intro{margin:0 0 18px;padding:14px 16px;background:var(--bg-secondary,#1b1b1b);border:1px solid var(--border,#2a2a2a);border-radius:8px}
    .vg-intro p{margin:0 0 8px;color:var(--text,#ddd);line-height:1.5}
    .vg-chan-desc{color:var(--text-muted,#aaa);font-size:.88em;margin:2px 0 5px;max-width:680px;line-height:1.4}
    .vg-modal{position:fixed;inset:0;background:rgba(0,0,0,.8);display:none;align-items:center;justify-content:center;z-index:1000;padding:24px}
    .vg-modal.open{display:flex}
    .vg-modal-box{width:min(960px,95vw);background:var(--bg,#111);border:1px solid var(--border,#333);border-radius:10px;overflow:hidden}
    .vg-modal-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 14px;border-bottom:1px solid var(--border,#333)}
    .vg-modal-title{color:var(--text,#eee);font-size:.95em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .vg-modal-close{background:transparent;border:none;color:var(--text,#eee);font-size:1.2em;cursor:pointer;padding:2px 8px}
    .vg-modal-player{aspect-ratio:16/9;background:#000}
    .vg-player-frame{width:100%;height:100%;border:0;display:block}
    `;
    document.head.appendChild(s);
}

/* ── View module ───────────────────────────────────────────────────── */

export default {
    init(el) {
        container = el;
        injectStyles();
        el.innerHTML = `
            <div class="vg-root"><p class="vg-empty">Loading…</p></div>
            <div class="vg-modal">
                <div class="vg-modal-box">
                    <div class="vg-modal-bar">
                        <span class="vg-modal-title"></span>
                        <button class="vg-modal-close" title="Close">✕</button>
                    </div>
                    <div class="vg-modal-player"></div>
                </div>
            </div>`;

        el.addEventListener('click', (e) => {
            const tab = e.target.closest('.vg-tab');
            if (tab) { activeTab = tab.dataset.tab; render(); return; }
            if (e.target.closest('#vg-refresh')) { doRefresh(); return; }
            // backdrop or close button
            if (e.target.classList.contains('vg-modal') || e.target.closest('.vg-modal-close')) {
                closePlayer(); return;
            }
            const card = e.target.closest('.vg-card');
            if (card) { openPlayer(card.dataset.vid, card.dataset.title); return; }
        });

        el.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') { closePlayer(); return; }
            const card = e.target.closest && e.target.closest('.vg-card');
            if (card && (e.key === 'Enter' || e.key === ' ')) {
                e.preventDefault();
                openPlayer(card.dataset.vid, card.dataset.title);
            }
        });
    },

    async show() {
        if (!data) data = await fetchVideos();
        // Pick a valid active tab
        const keys = new Set(['course', ...(data.channels || []).map(c => c.key)]);
        if (!keys.has(activeTab)) activeTab = 'course';
        if (activeTab === 'course' && !primary()?.course?.length) {
            activeTab = data.channels?.[0]?.key || 'course';
        }
        render();

        // Deep-link: window._viewSelect = '<videoId>' (in-app jump) or
        // hash #video-guide/<videoId>.
        let vid = null;
        if (typeof window._viewSelect === 'string') { vid = window._viewSelect; delete window._viewSelect; }
        else {
            const m = location.hash.match(/^#video-guide\/([A-Za-z0-9_-]{6,})$/);
            if (m) vid = m[1];
        }
        if (vid) openPlayer(vid, '');
    },

    hide() { closePlayer(); },
};
