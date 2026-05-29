// features/video-link.js — contextual help links (v1.5).
//
// Two rails, both using window._viewSelect (the cross-view jump rail chat.js
// uses): data-watch="<videoId>" jumps to the Video Guide + auto-plays;
// data-doc="<docPath>" jumps to the Help viewer + opens that doc. One delegated
// click handler + one style block installed on import → host pages need zero
// extra JS or CSS. Drop helpPills()/watchBadge() output anywhere.

import { switchView } from '../core/router.js';

export function openVideo(videoId) {
    if (!videoId) return;
    window._viewSelect = videoId;          // video-guide.show() reads this
    switchView('video-guide');
}

export function openDoc(docPath) {
    if (!docPath) return;
    window._viewSelect = docPath;          // help.show() reads this
    switchView('help');
}

export function watchBadge(videoId, label, dur) {
    const d = dur ? ` <span class="watch-badge-dur">${dur}</span>` : '';
    return `<button type="button" class="watch-badge" data-watch="${videoId}">`
        + `<span class="watch-badge-ico">▶</span> Watch: ${label}${d}</button>`;
}

// Combined "Help:" row — video pill + docs pill for a given page.
// (dur is accepted but not shown here — kept brief for the cramped sidebar.)
export function helpPills(label, { video, dur, doc, inline } = {}) {
    const v = video
        ? `<button type="button" class="watch-badge" data-watch="${video}">🎬 ${label}</button>`
        : '';
    const dc = doc
        ? `<button type="button" class="watch-badge" data-doc="${doc}">📖 ${label}</button>`
        : '';
    const cls = inline ? 'help-pills help-pills-inline' : 'help-pills';
    return `<div class="${cls}"><span class="help-pills-lbl">Help:</span>${v}${dc}</div>`;
}

let _ready = false;
function ensure() {
    if (_ready) return;
    _ready = true;
    if (!document.getElementById('watch-link-styles')) {
        const s = document.createElement('style');
        s.id = 'watch-link-styles';
        s.textContent = `
        .view-watch-row{margin:4px 0 10px}
        .help-pills{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:6px;margin:1rem 0 10px}
        .help-pills-inline{margin:0 0 0 auto;justify-content:flex-end}
        .help-pills-lbl{font-size:.82em;font-weight:600;color:var(--text,#fff)}
        .watch-badge{display:inline-flex;align-items:center;gap:5px;cursor:pointer;
          font:inherit;font-size:.82em;color:var(--text,#ddd);
          background:var(--bg-secondary,#1b1b1b);border:1px solid var(--border,#2a2a2a);
          border-radius:999px;padding:3px 10px;transition:border-color .15s,color .15s}
        .watch-badge:hover{border-color:var(--accent,#4a9eff);color:var(--accent,#4a9eff)}
        .watch-badge-ico{color:#ff4444;font-size:.9em}
        .doc-badge-ico{color:#4a9eff;font-size:.9em}
        .watch-badge-dur{opacity:.6;font-variant-numeric:tabular-nums}`;
        document.head.appendChild(s);
    }
    document.addEventListener('click', (e) => {
        const w = e.target.closest('[data-watch]');
        if (w) { e.preventDefault(); openVideo(w.getAttribute('data-watch')); return; }
        const d = e.target.closest('[data-doc]');
        if (d) { e.preventDefault(); openDoc(d.getAttribute('data-doc')); return; }
    });
}
ensure();
