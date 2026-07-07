// views/palace/knowledge.js - Mind › Human Knowledge, palace edition (L3:
// reference data in sub-chunked groups — label = old tab name, source +
// chunk_index preserve group identity for future neighbor-stitching).
import { renderSectionHeader, bindSectionHeader } from '../../shared/section-header.js';
import { helpPills } from '../../features/video-link.js';
import { renderScopeSidebar, bindScopeSidebar } from '../../shared/scope-sidebar.js';
import { listScopes } from '../../shared/scope-api.js';
import { escHtml, escAttr, scopeForChatTab, subscribeMindDomain } from '../../shared/mind-common.js';
import * as ui from '../../ui.js';
import { PALACE_TABS, SCOPE_ENDPOINT, palaceGet, chunkCard, bindChunkCards } from './common.js';

const SCOPE_KEY = 'memory_scope';
const DOMAIN = 'knowledge';
const PAGE = 50;

let container = null;
let scope = 'default';
let scopes = [];
let unsub = null;
let _search = '';
let _offset = 0;
let _searchTimer = null;

export default {
    init(el) { container = el; },
    async show() {
        if (!unsub) unsub = subscribeMindDomain(DOMAIN, () => scope, () => container?.offsetParent !== null, renderList);
        if (window._mindScope) { scope = window._mindScope; delete window._mindScope; }
        else { const s = await scopeForChatTab(SCOPE_KEY); if (s) scope = s; }
        delete window._mindTab;
        scopes = await listScopes(SCOPE_ENDPOINT);
        render();
    },
    hide() { if (unsub) { unsub(); unsub = null; } }
};

function content() { return container?.querySelector('#pal-kn-content'); }

function render() {
    if (!container) return;
    container.innerHTML = `
        ${renderSectionHeader({ tabs: PALACE_TABS, active: 'knowledge', help: helpPills('Knowledge', { doc: 'MEMORY.md', inline: true }), status: '\u{1F3DB}️ Mind Palace — reference knowledge (L3). Imported tabs keep their names as labels; sources stay grouped.' })}
        <div class="two-panel">
            ${renderScopeSidebar(scopes, scope)}
            <div class="panel-right">
                <div class="view-body view-scroll" id="pal-kn-content"></div>
            </div>
        </div>`;
    bindSectionHeader(container);
    bindScopeSidebar(container, {
        onScopeChange: (s) => { scope = s; _search = ''; _offset = 0; render(); },
        onChanged: async (s) => { scope = s || 'default'; _search = ''; _offset = 0; scopes = await listScopes(SCOPE_ENDPOINT); render(); },
    });
    renderList();
}

async function renderList() {
    const el = content();
    if (!el) return;
    const params = new URLSearchParams({ scope, layer: 'knowledge', limit: PAGE, offset: _offset });
    if (_search) params.set('q', _search);
    let data;
    try {
        data = await palaceGet(`chunks?${params}`);
    } catch (e) {
        el.innerHTML = `<div class="mind-empty">Failed to load: ${escHtml(e.message)}</div>`;
        return;
    }
    const chunks = data.chunks || [];
    el.innerHTML = `
        <div class="mind-toolbar">
            <input type="search" id="pal-kn-search" class="palace-search" placeholder="Search knowledge…" value="${escAttr(_search)}">
            <span class="palace-count">${data.total} chunks</span>
        </div>
        ${chunks.length
            ? `<div class="palace-chunk-list">${chunks.map(c => chunkCard(c, { showLayer: false })).join('')}</div>`
            : `<div class="mind-empty">${_search ? 'No matches' : 'No knowledge chunks in this scope'}</div>`}
        ${(!_search && _offset + PAGE < data.total)
            ? `<div class="palace-more-wrap"><button class="mind-btn" id="pal-kn-more">Load more (${data.total - _offset - PAGE} older)</button></div>`
            : ''}
    `;
    const searchBox = el.querySelector('#pal-kn-search');
    searchBox?.addEventListener('input', () => {
        clearTimeout(_searchTimer);
        _searchTimer = setTimeout(() => { _search = searchBox.value.trim(); _offset = 0; renderList(); }, 300);
    });
    el.querySelector('#pal-kn-more')?.addEventListener('click', () => { _offset += PAGE; renderList(); });
    bindChunkCards(el, renderList, ui);
}
