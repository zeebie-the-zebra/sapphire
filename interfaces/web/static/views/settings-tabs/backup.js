// settings-tabs/backup.js - Backup management
import * as ui from '../../ui.js';

let backups = { daily: [], weekly: [], monthly: [], manual: [] };
let expanded = {};

export default {
    id: 'backup',
    name: 'Backup',
    icon: '\uD83D\uDCBE',
    description: 'Automatic and manual backups of user data',
    keys: ['BACKUPS_ENABLED', 'BACKUPS_HOUR', 'BACKUPS_KEEP_DAILY', 'BACKUPS_KEEP_WEEKLY', 'BACKUPS_KEEP_MONTHLY', 'BACKUPS_KEEP_MANUAL', 'BACKUPS_MAX_SIZE_WARN_MB'],

    render(ctx) {
        return `
            <div id="backup-restore-banner"></div>
            ${ctx.renderFields(this.keys)}

            <div class="backup-hero">
                <button class="backup-now-btn" id="backup-now">Backup Now</button>
                <div class="backup-stats" id="backup-stats"></div>
            </div>

            <div class="backup-info" style="margin:16px 0;padding:12px 16px;background:var(--bg-secondary);border-radius:8px;font-size:var(--font-sm);line-height:1.6">
                <div style="margin-bottom:8px"><strong>Included in backups:</strong></div>
                <div style="color:var(--text-secondary)">
                    Chat history, prompts, toolsets, spices, scheduled tasks,
                    settings, memories, knowledge, AI notes, user plugins,
                    plugin state, and avatars
                </div>
                ${window.__managed ? `
                <div style="margin:10px 0 4px"><strong>Also included (managed mode):</strong></div>
                <div style="color:var(--text-secondary)">
                    API keys and credentials (LLM keys, email passwords, bitcoin wallets)
                    are stored inside your data volume and included in backups
                </div>
                ` : `
                <div style="margin:10px 0 4px"><strong>Not included:</strong></div>
                <div style="color:var(--text-muted)">
                    API keys and credentials (LLM keys, email passwords, SSH servers,
                    bitcoin wallets) are stored separately at ~/.config/sapphire/ for
                    security and are not part of the backup archive
                </div>
                `}
            </div>

            <div class="backup-section-divider" style="margin-top:16px">
                <h4 style="margin:0 0 8px;font-size:var(--font-sm)">Exclude from backups</h4>
                <div style="font-size:var(--font-xs);color:var(--text-muted);margin-bottom:6px;line-height:1.7">
                    Skip folders or files you don't want backed up &mdash; one per line.<br>
                    Type a folder name to skip it &nbsp;&rarr;&nbsp; <code>piper-voices</code> &nbsp;&middot;&nbsp; <code>rag</code><br>
                    Use a star <code>*</code> for "anything" &nbsp;&rarr;&nbsp; <code>*.log</code> (all log files) &nbsp;&middot;&nbsp; <code>history/*</code> (everything in history)<br>
                    Saves automatically. Your passwords and keys are never backed up either way.
                </div>
                <textarea id="backup-excludes" rows="4" spellcheck="false"
                    style="width:100%;font-family:var(--font-mono,monospace);font-size:var(--font-xs);padding:8px;background:var(--bg-tertiary);border:1px solid var(--border);border-radius:6px;color:var(--text-bright);resize:vertical;box-sizing:border-box"
                    placeholder="rag/*&#10;*.log"></textarea>
                <div style="display:flex;gap:10px;align-items:center;margin-top:8px;flex-wrap:wrap">
                    <button class="btn-sm" id="backup-checksize">Check size</button>
                    <span id="backup-size-result" style="font-size:var(--font-sm);color:var(--text-secondary)"></span>
                </div>
                <div id="backup-size-breakdown" style="margin-top:8px"></div>
            </div>

            <div class="backup-section-divider" style="margin-top:16px">
                <h4 style="margin:0 0 8px;font-size:var(--font-sm)">Encryption</h4>
                <div id="backup-enc"></div>
            </div>

            <div class="backup-section-divider">
                <h4 style="margin:0 0 10px;font-size:var(--font-sm)">Backup Files</h4>
                <div id="backup-lists"></div>
            </div>

            <div class="backup-section-divider" style="margin-top:16px">
                <h4 style="margin:0 0 8px;font-size:var(--font-sm)">Restore from a file</h4>
                <div style="font-size:var(--font-xs);color:var(--text-muted);margin-bottom:6px;line-height:1.6">
                    Upload a backup (<code>.tar.gz</code> or <code>.sapphirebak</code>) of a <code>user/</code> folder. Sapphire checks it, then restarts and swaps it in. Your current data is kept as <code>user.old</code> for rollback.
                </div>
                <button class="btn-sm" id="backup-restore-upload">Choose a backup file…</button>
                <input type="file" id="backup-restore-file" accept=".tar.gz,.gz,.sapphirebak" style="display:none">
            </div>
        `;
    },

    async attachListeners(ctx, el) {
        await this.loadBackups(el);
        await this.loadEncryption(el);
        await this.loadRestoreResult(el);

        // Exclude patterns — prefill from saved, AUTO-SAVE on blur + debounced
        // typing (no Save Changes needed; backups read it live).
        const exTa = el.querySelector('#backup-excludes');
        if (exTa) {
            const cur = ctx.getValue('BACKUPS_EXCLUDE_PATTERNS');
            const arr = Array.isArray(cur) ? cur : (typeof cur === 'string' ? cur.split('\n') : []);
            exTa.value = arr.join('\n');

            let saveT = null, lastSaved = exTa.value;
            const saveExcludes = async () => {
                if (exTa.value === lastSaved) return;
                lastSaved = exTa.value;
                const lines = exTa.value.split('\n').map(s => s.trim()).filter(Boolean);
                try {
                    await fetch('/api/settings/batch', {
                        method: 'PUT',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || ''
                        },
                        body: JSON.stringify({ settings: { BACKUPS_EXCLUDE_PATTERNS: lines } })
                    });
                    if (ctx.settings) ctx.settings.BACKUPS_EXCLUDE_PATTERNS = lines;
                } catch (_) { lastSaved = null; }  // allow retry on failure
            };
            exTa.addEventListener('input', () => { clearTimeout(saveT); saveT = setTimeout(saveExcludes, 1200); });
            exTa.addEventListener('blur', () => { clearTimeout(saveT); saveExcludes(); });
        }

        el.querySelector('#backup-checksize')?.addEventListener('click', async () => {
            const btn = el.querySelector('#backup-checksize');
            const result = el.querySelector('#backup-size-result');
            const breakdown = el.querySelector('#backup-size-breakdown');
            const patterns = (exTa?.value || '').split('\n').map(s => s.trim()).filter(Boolean);
            btn.disabled = true; result.textContent = 'Calculating…';
            try {
                const res = await fetch('/api/backup/estimate', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ patterns })
                });
                const r = await res.json();
                result.innerHTML = `Backup size: <strong>${fmtSize(r.total_bytes)}</strong>`
                    + (r.excluded_bytes ? ` <span style="color:var(--text-muted)">(excluded ${fmtSize(r.excluded_bytes)})</span>` : '')
                    + (r.over_warn ? ` <span style="color:var(--danger,#e06c6c)">⚠ over ${r.warn_mb} MB — trim excludes</span>` : '');
                const THRESH = 200 * 1024;  // hide the tiny stuff behind "see all"
                const all = r.breakdown || [];
                const big = all.filter(b => b.bytes >= THRESH);
                const small = all.filter(b => b.bytes < THRESH);
                const row = b => `<div style="display:flex;justify-content:space-between;gap:12px;font-size:var(--font-xs);padding:3px 0;border-bottom:1px solid var(--border)"><span style="font-family:var(--font-mono,monospace);overflow:hidden;text-overflow:ellipsis">${esc(b.name)}</span><span style="white-space:nowrap">${fmtSize(b.bytes)}</span></div>`;
                breakdown.innerHTML = (big.map(row).join('') || '<div style="font-size:var(--font-xs);color:var(--text-muted)">Nothing over 200 KB</div>')
                    + (small.length
                        ? `<div id="bk-small" style="display:none">${small.map(row).join('')}</div>`
                          + `<button id="bk-seeall" style="background:none;border:none;color:var(--trim);cursor:pointer;font-size:var(--font-xs);padding:6px 0">See all (${small.length} smaller)</button>`
                        : '');
                const seeAll = breakdown.querySelector('#bk-seeall');
                if (seeAll) seeAll.addEventListener('click', () => {
                    const sm = breakdown.querySelector('#bk-small');
                    const open = sm.style.display !== 'none';
                    sm.style.display = open ? 'none' : 'block';
                    seeAll.textContent = open ? `See all (${small.length} smaller)` : 'Show less';
                });
            } catch (e) {
                result.textContent = 'Estimate failed';
            } finally { btn.disabled = false; }
        });

        // Restore from an uploaded file
        const upBtn = el.querySelector('#backup-restore-upload');
        const upInput = el.querySelector('#backup-restore-file');
        upBtn?.addEventListener('click', () => upInput.click());
        upInput?.addEventListener('change', async () => {
            const f = upInput.files[0];
            if (!f) return;
            let password = '';
            if (f.name.endsWith('.sapphirebak')) {
                password = await passwordPrompt(`"${f.name}" looks encrypted. Enter the backup password:`);
                if (password === null) { upInput.value = ''; return; }
            }
            if (!confirm(`Restore from "${f.name}"?\n\nThis RESTARTS Sapphire and replaces your current user data.\nYour current data is kept as "user.old" for rollback.`)) {
                upInput.value = ''; return;
            }
            const fd = new FormData();
            fd.append('file', f);
            fd.append('password', password || '');
            try {
                const res = await fetch('/api/backup/restore-upload', { method: 'POST', headers: { 'X-CSRF-Token': csrf() }, body: fd });
                if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || 'Restore failed'); }
                ui.showToast('Restoring… Sapphire is restarting. This page will reconnect shortly.', 'success', 9000);
            } catch (e) { ui.showToast(`Restore failed: ${e.message}`, 'error'); }
            finally { upInput.value = ''; }
        });

        el.querySelector('#backup-now')?.addEventListener('click', async () => {
            const btn = el.querySelector('#backup-now');
            btn.disabled = true;
            btn.textContent = 'Creating...';
            try {
                const res = await fetch('/api/backup/create', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ type: 'manual' })
                });
                if (res.ok) {
                    const data = await res.json();
                    ui.showToast(`Backup created: ${data.filename}`, 'success');
                    await this.loadBackups(el);
                } else {
                    ui.showToast('Backup failed', 'error');
                }
            } catch (e) { ui.showToast('Backup failed', 'error'); }
            finally { btn.disabled = false; btn.textContent = 'Backup Now'; }
        });
    },

    async loadBackups(el) {
        try {
            const res = await fetch('/api/backup/list');
            if (res.ok) {
                const data = await res.json();
                backups = data.backups || {};
            }
        } catch {}
        this.renderBackups(el);
    },

    async loadRestoreResult(el) {
        const box = el.querySelector('#backup-restore-banner');
        if (!box) return;
        let r = null;
        try { r = await (await fetch('/api/backup/restore-result')).json(); } catch {}
        if (!r || r.ok === undefined) { box.innerHTML = ''; return; }
        const ok = r.ok === true;
        const src = (r.source || '').replace(/^backup:|^upload:/, '');
        box.innerHTML = `
            <div style="display:flex;align-items:flex-start;gap:10px;margin-bottom:14px;padding:10px 12px;border-radius:8px;line-height:1.5;
                background:${ok ? 'rgba(108,204,108,0.12)' : 'rgba(224,108,108,0.12)'};
                border:1px solid ${ok ? 'rgba(108,204,108,0.5)' : 'var(--danger,#e06c6c)'}">
                <div style="flex:1;font-size:var(--font-sm)">
                    ${ok
                        ? `&#10003; <strong>Restore complete.</strong> Your data was restored${src ? ` from <code>${esc(src)}</code>` : ''}. The previous data is kept in <code>user.old</code>.`
                        : `&#10007; <strong>Restore failed</strong> &mdash; your data was left untouched (rolled back).${r.error ? ` <span style="color:var(--text-secondary)">${esc(r.error)}</span>` : ''}`}
                </div>
                <button class="btn-icon" id="restore-banner-x" title="Dismiss" style="flex:none">&#10005;</button>
            </div>`;
        box.querySelector('#restore-banner-x')?.addEventListener('click', async () => {
            box.innerHTML = '';
            try { await fetch('/api/backup/restore-result', { method: 'DELETE', headers: { 'X-CSRF-Token': csrf() } }); } catch {}
        });
    },

    async loadEncryption(el) {
        const box = el.querySelector('#backup-enc');
        if (!box) return;
        let s = { enabled: false, has_password: false };
        try { s = await (await fetch('/api/backup/encryption-status')).json(); } catch {}

        box.innerHTML = `
            ${s.password_status === 'unreadable' ? `
            <div style="background:rgba(224,108,108,0.2);border:1px solid var(--danger,#e06c6c);border-radius:6px;padding:10px 12px;font-size:var(--font-xs);line-height:1.6;margin-bottom:10px">
                &#9888; <strong>Encryption is ON but the saved password can't be read.</strong> This usually means the config was reset or you moved machines. <strong>New backups are NOT being encrypted.</strong> Re-enter your password below to fix it. (Your existing encrypted backups are fine &mdash; the password still decrypts them.)
            </div>` : ''}
            <div style="font-size:var(--font-xs);color:var(--text-muted);line-height:1.6;margin-bottom:8px">
                Encrypts every backup with a password only you know. Required for offsite backups.
            </div>
            <div style="background:rgba(224,108,108,0.12);border:1px solid var(--danger,#e06c6c);border-radius:6px;padding:10px;font-size:var(--font-xs);line-height:1.6;margin-bottom:10px">
                &#9888; <strong>Write your password down.</strong> If you lose it, your encrypted backups are <strong>gone for good</strong> &mdash; there is no recovery. Not even us.
            </div>
            <label style="display:flex;align-items:center;gap:8px;font-size:var(--font-sm);margin-bottom:10px">
                <input type="checkbox" id="enc-toggle" ${s.enabled ? 'checked' : ''} ${s.has_password ? '' : 'disabled'}>
                Encrypt backups ${s.has_password ? '' : '<span style="color:var(--text-muted)">&mdash; set a password first</span>'}
            </label>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px">
                <input type="password" id="enc-pw" autocomplete="new-password" placeholder="${s.has_password ? 'Change password…' : 'Set a password…'}"
                    style="flex:1;min-width:180px;padding:7px 10px;background:var(--bg-tertiary);border:1px solid var(--border);border-radius:6px;color:var(--text-bright);box-sizing:border-box">
                <button class="btn-sm" id="enc-setpw">${s.has_password ? 'Change' : 'Set password'}</button>
                ${s.has_password ? '<button class="btn-sm" id="enc-test">Test</button>' : ''}
                <span id="enc-msg" style="font-size:var(--font-xs);color:var(--text-secondary)"></span>
            </div>
            ${s.has_password ? '<div style="font-size:var(--font-xs);color:var(--text-muted)">&#10003; Password is set.</div>' : ''}
        `;

        box.querySelector('#enc-toggle')?.addEventListener('change', async (e) => {
            const on = e.target.checked;
            try {
                const r = await fetch('/api/settings/batch', {
                    method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
                    body: JSON.stringify({ settings: { BACKUPS_ENCRYPT: on } })
                });
                if (!r.ok) throw new Error();
                ui.showToast(on ? 'Backups will be encrypted' : 'Encryption turned off', 'success');
            } catch { e.target.checked = !on; ui.showToast('Failed to change setting', 'error'); }
        });

        box.querySelector('#enc-setpw')?.addEventListener('click', async () => {
            const pw = box.querySelector('#enc-pw').value;
            const msg = box.querySelector('#enc-msg');
            if (!pw) { msg.textContent = 'Enter a password first'; return; }
            try {
                const r = await fetch('/api/backup/password', {
                    method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
                    body: JSON.stringify({ password: pw })
                });
                if (!r.ok) throw new Error();
                ui.showToast('Password saved — write it down!', 'success');
                await this.loadEncryption(el);
            } catch { msg.textContent = 'Failed to save'; }
        });

        box.querySelector('#enc-test')?.addEventListener('click', async () => {
            const msg = box.querySelector('#enc-msg');
            msg.textContent = 'Testing…';
            try {
                const r = await (await fetch('/api/backup/test-encryption', {
                    method: 'POST', headers: { 'X-CSRF-Token': csrf() }
                })).json();
                msg.innerHTML = r.ok
                    ? '<span style="color:#6c6">&#10003; Encryption works</span>'
                    : `<span style="color:var(--danger,#e06c6c)">&#10007; ${esc(r.error || 'failed')}</span>`;
            } catch { msg.textContent = 'Test failed'; }
        });
    },

    renderBackups(el) {
        const lists = el.querySelector('#backup-lists');
        const stats = el.querySelector('#backup-stats');
        if (!lists) return;

        let totalSize = 0, totalCount = 0;
        for (const type of ['daily', 'weekly', 'monthly', 'manual']) {
            for (const b of (backups[type] || [])) {
                totalSize += b.size || 0;
                totalCount++;
            }
        }

        if (stats) stats.textContent = `${totalCount} backups \u00B7 ${fmtSize(totalSize)}`;

        lists.innerHTML = ['daily', 'weekly', 'monthly', 'manual'].map(type => {
            const items = backups[type] || [];
            const isOpen = expanded[type];
            return `
                <div class="backup-type-section">
                    <div class="backup-type-header" data-type="${type}">
                        <span class="accordion-arrow" style="transform:${isOpen ? 'rotate(90deg)' : 'none'}">\u25B6</span>
                        <span class="backup-type-title">${type[0].toUpperCase() + type.slice(1)}</span>
                        <span class="backup-type-count">${items.length}</span>
                    </div>
                    <div class="backup-type-body" style="display:${isOpen ? 'block' : 'none'}">
                        ${items.length ? items.map(b => `
                            <div class="backup-item" data-filename="${esc(b.filename)}">
                                <span class="backup-item-date">${b.encrypted ? '🔒 ' : ''}${b.date} ${b.time}</span>
                                <span class="backup-item-size">${fmtSize(b.size)}</span>
                                <div class="backup-item-actions">
                                    <button class="btn-icon backup-restore" data-filename="${esc(b.filename)}" data-enc="${b.encrypted ? '1' : ''}" title="Restore this backup (restarts Sapphire)">\u21BB</button>
                                    <a class="btn-icon backup-dl" href="/api/backup/download/${encodeURIComponent(b.filename)}" download title="Download">\u2B07</a>
                                    <button class="btn-icon danger backup-del" data-filename="${esc(b.filename)}" title="Delete">\u2715</button>
                                </div>
                            </div>
                        `).join('') : '<div class="backup-empty">No backups</div>'}
                    </div>
                </div>
            `;
        }).join('');

        // Accordion toggles
        lists.querySelectorAll('.backup-type-header').forEach(header => {
            header.addEventListener('click', () => {
                const type = header.dataset.type;
                expanded[type] = !expanded[type];
                const body = header.nextElementSibling;
                const arrow = header.querySelector('.accordion-arrow');
                body.style.display = expanded[type] ? 'block' : 'none';
                arrow.style.transform = expanded[type] ? 'rotate(90deg)' : 'none';
            });
        });

        // Delete buttons
        lists.querySelectorAll('.backup-del').forEach(btn => {
            btn.addEventListener('click', async () => {
                const filename = btn.dataset.filename;
                if (!confirm(`Delete ${filename}?`)) return;
                try {
                    const res = await fetch(`/api/backup/delete/${encodeURIComponent(filename)}`, { method: 'DELETE' });
                    if (res.ok) {
                        ui.showToast('Deleted', 'success');
                        await this.loadBackups(el);
                    }
                } catch { ui.showToast('Delete failed', 'error'); }
            });
        });

        // Restore buttons — destructive: restarts Sapphire and swaps user/.
        lists.querySelectorAll('.backup-restore').forEach(btn => {
            btn.addEventListener('click', async () => {
                const filename = btn.dataset.filename;
                let password = '';
                if (btn.dataset.enc === '1') {
                    password = await passwordPrompt(`"${filename}" is encrypted. Enter the backup password:`);
                    if (password === null) return;
                }
                if (!confirm(`Restore "${filename}"?\n\nThis RESTARTS Sapphire and replaces your current user data.\nYour current data is kept as "user.old" for rollback.`)) return;
                try {
                    const res = await fetch('/api/backup/restore', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
                        body: JSON.stringify({ filename, password })
                    });
                    if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || 'Restore failed'); }
                    ui.showToast('Restoring… Sapphire is restarting. This page will reconnect shortly.', 'success', 9000);
                } catch (e) { ui.showToast(`Restore failed: ${e.message}`, 'error'); }
            });
        });
    }
};

function fmtSize(bytes) {
    if (!bytes) return '0 B';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
}

function esc(s) {
    if (!s) return '';
    return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function csrf() {
    return document.querySelector('meta[name="csrf-token"]')?.content || '';
}

// Masked password prompt (window.prompt shows plaintext). Resolves the value, or
// null if cancelled.
function passwordPrompt(message) {
    return new Promise((resolve) => {
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay active';
        overlay.innerHTML = `
            <div class="modal-base" style="max-width:420px;width:92vw;padding:18px">
                <div style="font-size:var(--font-sm);margin-bottom:10px;line-height:1.5">${esc(message)}</div>
                <input type="password" id="pwp-input" autocomplete="off"
                    style="width:100%;padding:8px 10px;background:var(--bg-tertiary);border:1px solid var(--border);border-radius:6px;color:var(--text-bright);box-sizing:border-box;margin-bottom:12px">
                <div class="modal-actions" style="display:flex;gap:8px;justify-content:flex-end">
                    <button class="btn-sm" id="pwp-cancel">Cancel</button>
                    <button class="btn-sm" id="pwp-ok">OK</button>
                </div>
            </div>`;
        document.body.appendChild(overlay);
        const input = overlay.querySelector('#pwp-input');
        input.focus();
        const done = (val) => { document.removeEventListener('keydown', onKey); overlay.remove(); resolve(val); };
        const onKey = (e) => {
            if (e.key === 'Escape') done(null);
            else if (e.key === 'Enter') done(input.value);
        };
        document.addEventListener('keydown', onKey);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) done(null); });
        overlay.querySelector('#pwp-cancel').addEventListener('click', () => done(null));
        overlay.querySelector('#pwp-ok').addEventListener('click', () => done(input.value));
    });
}
