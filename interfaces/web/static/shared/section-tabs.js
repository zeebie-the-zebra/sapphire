// shared/section-tabs.js - One tab strip for every section (Persona, Triggers,
// Mind, …). Takes a tabs config + active id; navigates via switchView. Replaces
// the near-duplicate persona-tabs.js / trigger-tabs.js.
//
// Transitional: elements carry BOTH `.section-tabs`/`.section-tab` and the legacy
// `.persona-tabs`/`.persona-tab` classes so existing CSS applies unchanged while
// sections migrate (decision #4: rename-as-we-go with alias).
import { switchView } from '../core/router.js';

/**
 * @param {{id:string,label:string,icon?:string}[]} tabs
 * @param {string} activeId
 * @param {string} rightSlot - optional HTML pinned right (e.g. help pills)
 */
export function renderSectionTabs(tabs, activeId, rightSlot = '') {
    return `<div class="section-tabs persona-tabs">
        ${tabs.map(t => `<button class="section-tab persona-tab${t.id === activeId ? ' active' : ''}" data-view="${t.id}">${t.icon ? t.icon + ' ' : ''}${t.label}</button>`).join('')}
        ${rightSlot}
    </div>`;
}

/** Delegated tab clicks → switchView. Call once per render. */
export function bindSectionTabs(container) {
    const tabs = container.querySelector('.section-tabs');
    if (!tabs) return;
    tabs.addEventListener('click', e => {
        const btn = e.target.closest('.section-tab');
        if (!btn) return;
        const viewId = btn.dataset.view;
        if (viewId) switchView(viewId);
    });
}
