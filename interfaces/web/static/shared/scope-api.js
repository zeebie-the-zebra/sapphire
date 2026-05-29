// shared/scope-api.js - Mind scope operations.
//
// Scope model: a scope is a MIND-WIDE context. Create/delete happen in LOCKSTEP
// across every Mind scope domain (memory, knowledge, people, goals, + any plugin
// mind-scopes) so scopes stay aligned — never a work-memory without a work-goals.
// SELECTION is per-view (each view tracks its own selected scope).
//
// The domain list is data-driven off /api/init `scope_declarations` filtered to
// nav_target "mind:*" (mirrors the old mind.js delete) so plugin mind-scopes are
// swept automatically — no hardcoded URL list.
import { getInitData } from './init-data.js';

function csrfHeaders(extra = {}) {
    const token = document.querySelector('meta[name="csrf-token"]')?.content || '';
    return { 'X-CSRF-Token': token, ...extra };
}

/** All Mind scope domains from /api/init: [{nav_target, endpoint, ...}, ...]. */
export async function mindScopeDomains() {
    const init = await getInitData().catch(() => null);
    return (init?.scope_declarations || []).filter(d => d.nav_target?.startsWith('mind:'));
}

/** Scope endpoint for a Mind tab id, e.g. 'memories' → '/api/memory/scopes'. */
export async function scopeEndpointForTab(tabId) {
    const decls = await mindScopeDomains();
    return decls.find(d => d.nav_target === `mind:${tabId}`)?.endpoint || null;
}

/** List scopes for one domain endpoint → [{name, count?}, ...]. Fails to []. */
export async function listScopes(endpoint) {
    if (!endpoint) return [];
    try {
        const r = await fetch(endpoint, { credentials: 'same-origin' });
        if (!r.ok) return [];
        const data = await r.json();
        return data.scopes || [];
    } catch { return []; }
}

/** Lockstep create: POST {name} to EVERY Mind scope domain. Tolerant; true if any ok. */
export async function createScopeEverywhere(name) {
    const decls = await mindScopeDomains();
    const results = await Promise.allSettled(decls.map(d =>
        fetch(d.endpoint, {
            method: 'POST',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            credentials: 'same-origin',
            body: JSON.stringify({ name }),
        })
    ));
    return results.some(r => r.status === 'fulfilled' && r.value.ok);
}

/** Lockstep delete: DELETE {confirm:'DELETE'} from EVERY Mind scope domain. */
export async function deleteScopeEverywhere(name) {
    const decls = await mindScopeDomains();
    const enc = encodeURIComponent(name);
    const results = await Promise.allSettled(decls.map(d =>
        fetch(`${d.endpoint}/${enc}`, {
            method: 'DELETE',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            credentials: 'same-origin',
            body: JSON.stringify({ confirm: 'DELETE' }),
        })
    ));
    return results.some(r => r.status === 'fulfilled' && r.value.ok);
}
