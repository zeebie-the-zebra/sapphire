// views/knowledge.js - Mind › Human Knowledge. Your reference library (files &
// notes). Shares the knowledge scope domain + renderer with AI Knowledge.
import { renderSectionHeader, bindSectionHeader } from '../shared/section-header.js';
import { helpPills } from '../features/video-link.js';
import { renderScopeSidebar, bindScopeSidebar } from '../shared/scope-sidebar.js';
import { listScopes } from '../shared/scope-api.js';
import { MIND_TABS, scopeForChatTab, subscribeMindDomain } from '../shared/mind-common.js';
import { renderKnowledge } from '../shared/mind-knowledge.js';

const SCOPE_KEY = 'knowledge_scope';
const DOMAIN = 'knowledge';
const SCOPE_ENDPOINT = '/api/knowledge/scopes';
const TAB_TYPE = 'user';

let container = null;
let scope = 'default';
let scopes = [];
let unsub = null;

export default {
    init(el) { container = el; },
    async show() {
        if (!unsub) unsub = subscribeMindDomain(DOMAIN, () => scope, () => container?.offsetParent !== null,
            () => renderKnowledge(content(), TAB_TYPE, scope));
        if (window._mindScope) { scope = window._mindScope; delete window._mindScope; }
        else { const s = await scopeForChatTab(SCOPE_KEY); if (s) scope = s; }
        delete window._mindTab;
        scopes = await listScopes(SCOPE_ENDPOINT);
        render();
    },
    hide() { if (unsub) { unsub(); unsub = null; } }
};

function content() { return container?.querySelector('#kn-content'); }

function render() {
    if (!container) return;
    container.innerHTML = `
        ${renderSectionHeader({ tabs: MIND_TABS, active: 'knowledge', help: helpPills('Human Knowledge', { video: 'I3g3tzukpV0', doc: 'KNOWLEDGE.md', inline: true }), status: 'Your reference library — files & notes Sapphire can search but cannot edit.' })}
        <div class="two-panel">
            ${renderScopeSidebar(scopes, scope)}
            <div class="panel-right">
                <div class="view-body view-scroll" id="kn-content"></div>
            </div>
        </div>`;
    bindSectionHeader(container);
    bindScopeSidebar(container, {
        onScopeChange: (s) => { scope = s; render(); },
        onChanged: async (s) => { scope = s || 'default'; scopes = await listScopes(SCOPE_ENDPOINT); render(); },
    });
    renderKnowledge(content(), TAB_TYPE, scope);
}
