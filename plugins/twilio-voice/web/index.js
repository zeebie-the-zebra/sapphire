// Twilio Voice settings — multi-number account manager (A4).
// Uses the shared account-manager shell; accounts live in
// credentials_manager.twilio_accounts via /api/plugin/twilio-voice/accounts.
// Adding a number here does NOT make her answer it — answering is gated by an
// enabled rule in Triggers > Realtime that selects the number.

import { registerPluginSettings } from '/static/shared/plugin-registry.js';
import { createAccountManager } from '/static/shared/account-manager.js';

const API = '/api/plugin/twilio-voice/accounts';

const manager = createAccountManager({
    prefix: 'twv',
    entityName: 'Number',
    listEndpoint: API,
    listKey: 'accounts',
    deleteEndpoint: (scope) => `${API}/${encodeURIComponent(scope)}`,
    formatItem: (item) => ({
        name: item.number || item.scope,
        detail: item.configured
            ? `${item.sip_user}@${item.sip_domain}`
            : 'not configured',
    }),
    hint: 'Each entry is one Twilio phone number (SIP domain credential set). '
        + 'Sapphire only answers a number while an enabled Triggers > Realtime '
        + 'rule selects it — adding it here just makes it available.',
    addLabel: '+ Add Number',
    addPrompt: 'Name for this number (e.g. "default", "work"):',
    renderEditor: renderNumberEditor,
});


function renderNumberEditor(body, scope, item, helpers) {
    const s = item || {};
    body.innerHTML = `
        <div class="am-group">
            <label for="twv-domain">SIP Domain</label>
            <input type="text" id="twv-domain" value="${s.sip_domain || ''}" placeholder="yourname.sip.twilio.com">
            <div class="am-hint">The Twilio SIP domain this number's credential list belongs to.</div>
        </div>
        <div class="am-group">
            <label for="twv-user">SIP Username</label>
            <input type="text" id="twv-user" value="${s.sip_user || ''}" placeholder="sapphire">
        </div>
        <div class="am-group">
            <label for="twv-pass">SIP Password</label>
            <div class="am-row">
                <input type="password" id="twv-pass" placeholder="${s.configured ? 'Leave blank to keep existing...' : 'Enter password'}">
                <span class="am-action-btn${s.configured ? ' success' : ''}" style="cursor:default;padding:6px 12px;font-size:12px">
                    ${s.configured ? '✓ Stored' : 'Not set'}
                </span>
            </div>
            <div class="am-hint">The credential-list password from the Twilio console. Encrypted on disk.</div>
        </div>
        <div class="am-group">
            <label for="twv-number">Phone Number</label>
            <input type="text" id="twv-number" value="${s.number || ''}" placeholder="+15551234567">
            <div class="am-hint">The E.164 number, for display and call-event payloads.</div>
        </div>
        <div class="am-group">
            <label for="twv-greeting">Greeting</label>
            <input type="text" id="twv-greeting" value="${s.greeting || ''}" placeholder="Hey, this is Sapphire.">
            <div class="am-hint">Spoken on pickup. A Realtime rule's greeting overrides this per-rule.</div>
        </div>
        <div style="border-top:1px solid var(--border);padding-top:16px;margin-top:8px">
            <div style="font-size:14px;font-weight:600;color:var(--text);margin-bottom:4px">Outbound calling (optional)</div>
            <div class="am-hint" style="margin-bottom:12px">Lets Sapphire place calls from this number (the phone_call tool). From the Twilio Console dashboard — separate from the SIP credentials above.</div>
            <div class="am-group">
                <label for="twv-sid">Account SID</label>
                <input type="text" id="twv-sid" value="${s.account_sid || ''}" placeholder="AC...">
            </div>
            <div class="am-group" style="margin-top:12px">
                <label for="twv-token">Auth Token</label>
                <div class="am-row">
                    <input type="password" id="twv-token" placeholder="${s.rest_configured ? 'Leave blank to keep existing...' : 'Enter auth token'}">
                    <span class="am-action-btn${s.rest_configured ? ' success' : ''}" style="cursor:default;padding:6px 12px;font-size:12px">
                        ${s.rest_configured ? '✓ Stored' : 'Not set'}
                    </span>
                </div>
                <div class="am-hint">Encrypted on disk. Full-account secret — only needed for outbound.</div>
            </div>
        </div>
        <button type="button" class="am-action-btn" id="twv-save">Save</button>
    `;

    body.querySelector('#twv-save').addEventListener('click', async () => {
        const btn = body.querySelector('#twv-save');
        const payload = {
            scope,
            sip_domain: body.querySelector('#twv-domain').value.trim(),
            sip_user: body.querySelector('#twv-user').value.trim(),
            sip_pass: body.querySelector('#twv-pass').value.trim(),
            number: body.querySelector('#twv-number').value.trim(),
            greeting: body.querySelector('#twv-greeting').value.trim(),
            account_sid: body.querySelector('#twv-sid').value.trim(),
            auth_token: body.querySelector('#twv-token').value.trim(),
        };
        if (!payload.sip_domain || !payload.sip_user) {
            helpers.showResult(false, 'SIP domain and username are required');
            return;
        }
        btn.disabled = true;
        btn.textContent = 'Saving...';
        try {
            const res = await fetch(API, {
                method: 'POST',
                headers: helpers.csrfHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (!data.ok) throw new Error(data.error || 'Save failed');
            btn.textContent = 'Saved';
            btn.className = 'am-action-btn success';
            await manager.loadItems();
        } catch (e) {
            btn.textContent = 'Error';
            btn.className = 'am-action-btn error';
            helpers.showResult(false, e.message);
        }
        setTimeout(() => {
            btn.textContent = 'Save';
            btn.className = 'am-action-btn';
            btn.disabled = false;
        }, 3000);
    });
}


export default {
    name: 'twilio-voice',

    init() {
        registerPluginSettings({
            id: 'twilio-voice',
            name: 'Twilio Voice',
            icon: '📞',
            helpText: 'Phone numbers Sapphire can answer, one entry per Twilio SIP '
                + 'credential set. To make her actually answer a number, enable a '
                + 'rule for it in Triggers > Realtime.',
            render: (c) => manager.renderList(c),
            load: async () => { await manager.loadItems(); return {}; },
            save: async () => ({ success: true }),
            getSettings: () => ({}),
        });
    },

    destroy() {},
};
