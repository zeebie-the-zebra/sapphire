// shared/section-header.js - The reusable section header template. Slot-based:
// pass only the slots you use; omitted slots render nothing. Adding a new row
// type later is a new optional key — backwards compatible, existing callers
// untouched.
//
//   row 1:  [ tabs ]                         help (right)
//   row 2:  .section-status                  (if `status` provided)
//   row 3+: .section-row (each `rows` entry)  (optional, e.g. a timeline)
//
// Dynamic sections fill `.section-status` / a row's `id` on their poll-update;
// pass an empty string to render the container, then refill it by selector.
import { renderSectionTabs, bindSectionTabs } from './section-tabs.js';

/**
 * @param {object} o
 * @param {{id,label,icon?}[]} o.tabs
 * @param {string} o.active
 * @param {string} [o.help='']   - right-aligned HTML (e.g. helpPills(...))
 * @param {string} [o.status]    - row 2 HTML; pass '' to get an empty container
 * @param {(string|{id?:string,cls?:string,html?:string})[]} [o.rows=[]] - extra rows
 */
export function renderSectionHeader({ tabs, active, help = '', status, rows = [] } = {}) {
    let html = renderSectionTabs(tabs, active, help);
    if (status !== undefined) html += `<div class="section-status">${status}</div>`;
    for (const r of rows) {
        const id = r && r.id ? ` id="${r.id}"` : '';
        const cls = r && r.cls ? ` ${r.cls}` : '';
        const content = (r && r.html !== undefined) ? r.html : (typeof r === 'string' ? r : '');
        html += `<div class="section-row${cls}"${id}>${content}</div>`;
    }
    return html;
}

export function bindSectionHeader(container) {
    bindSectionTabs(container);
}
