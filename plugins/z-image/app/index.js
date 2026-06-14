// Z-Image Studio — user-driven generate UI. Tune prompts, CFG, seed, steps in
// real time without asking Sapphire to generate. Calls the plugin's
// POST /api/plugin/z-image/generate route.

let _container = null;
let _busy = false;

const CSRF = () => document.querySelector('meta[name="csrf-token"]')?.content || '';
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

export async function render(container) {
    _container = container;
    container.innerHTML = `
      <style>
        .zis-wrap { max-width: 1100px; margin: 0 auto; padding: 16px; }
        .zis-grid { display: grid; grid-template-columns: 360px 1fr; gap: 18px; align-items: start; }
        @media (max-width: 820px) { .zis-grid { grid-template-columns: 1fr; } }
        .zis-panel { background: var(--bg-elev, #1b1b22); border: 1px solid var(--border, #333);
                     border-radius: 10px; padding: 14px; }
        .zis-wrap h2 { margin: 0 0 4px; }
        .zis-sub { color: var(--text-dim, #9aa); font-size: 13px; margin: 0 0 14px; }
        .zis-row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }
        .zis-field { display: flex; flex-direction: column; gap: 4px; }
        .zis-field.grow { flex: 1 1 100%; }
        .zis-field label { font-size: 12px; color: var(--text-dim, #9aa); }
        .zis-field input, .zis-field textarea {
            background: var(--bg, #111); color: var(--text, #eee);
            border: 1px solid var(--border, #333); border-radius: 6px; padding: 7px 9px; font: inherit; }
        .zis-field textarea { min-height: 96px; resize: vertical; }
        .zis-field.w90 input { width: 84px; }
        .zis-check { display: flex; align-items: center; gap: 7px; font-size: 13px; margin-top: 12px; }
        .zis-btn { margin-top: 14px; width: 100%; padding: 11px; border: none; border-radius: 8px;
            background: var(--accent, #4a7dff); color: #fff; font-weight: 600; cursor: pointer; }
        .zis-btn:disabled { opacity: .55; cursor: default; }
        .zis-hint { font-size: 11px; color: var(--text-dim, #888); margin-top: 3px; }
        .zis-status { min-height: 20px; font-size: 13px; color: var(--text-dim, #9aa); margin-bottom: 10px; }
        .zis-status.err { color: #ff6b6b; }
        .zis-meta { font-size: 12px; color: var(--text-dim, #9aa); background: var(--bg, #111);
            border: 1px solid var(--border, #333); border-radius: 6px; padding: 8px 10px;
            margin-bottom: 12px; white-space: pre-wrap; word-break: break-word; }
        .zis-imgs { display: flex; flex-wrap: wrap; gap: 10px; }
        .zis-imgs figure { margin: 0; }
        .zis-imgs img { max-width: 100%; max-height: 70vh; border-radius: 8px; display: block;
            border: 1px solid var(--border, #333); cursor: zoom-in; }
        .zis-imgs figcaption { font-size: 11px; color: var(--text-dim, #888); margin-top: 3px;
            display: flex; gap: 10px; }
        .zis-imgs a { color: var(--accent, #4a7dff); text-decoration: none; }
      </style>
      <div class="zis-wrap">
        <h2>🖼️ Z-Image Studio</h2>
        <p class="zis-sub">Generate one-offs to tune prompts, CFG, seed and steps. Type your
          configured character names (set in Settings) and tick Expand to swap them for appearances.</p>
        <div class="zis-grid">
          <div class="zis-panel">
            <div class="zis-field grow">
              <label>Prompt</label>
              <textarea id="zis-prompt" placeholder="Sapphire, sitting by a window, soft light"></textarea>
            </div>
            <div class="zis-row">
              <div class="zis-field w90"><label>Count</label><input id="zis-count" type="number" min="1" value="1"></div>
              <div class="zis-field w90"><label>Steps</label><input id="zis-steps" type="number" min="1" placeholder="8"></div>
              <div class="zis-field w90"><label>CFG</label><input id="zis-cfg" type="number" step="0.1" placeholder="1.0"></div>
            </div>
            <div class="zis-row">
              <div class="zis-field w90"><label>Width</label><input id="zis-w" type="number" step="64" placeholder="1024"></div>
              <div class="zis-field w90"><label>Height</label><input id="zis-h" type="number" step="64" placeholder="1024"></div>
              <div class="zis-field grow"><label>Seed (blank = random)</label><input id="zis-seed" type="text" placeholder="random"></div>
            </div>
            <div class="zis-field grow" style="margin-top:10px;">
              <label>Negative prompt</label>
              <input id="zis-neg" type="text" placeholder="(inert at CFG 1.0)">
              <div class="zis-hint">Z-Image Turbo ignores the negative prompt at CFG≈1. Raise CFG to make it bite.</div>
            </div>
            <label class="zis-check"><input id="zis-expand" type="checkbox" checked> Expand me / you + keywords</label>
            <button id="zis-go" class="zis-btn">Generate</button>
          </div>
          <div class="zis-panel">
            <div id="zis-status" class="zis-status">Ready.</div>
            <div id="zis-meta" class="zis-meta" style="display:none;"></div>
            <div id="zis-imgs" class="zis-imgs"></div>
          </div>
        </div>
      </div>`;

    const go = container.querySelector('#zis-go');
    go.addEventListener('click', generate);
    container.querySelector('#zis-prompt').addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') generate();
    });
    container.querySelector('#zis-imgs').addEventListener('click', (e) => {
        if (e.target.tagName === 'IMG') window.open(e.target.src, '_blank');
    });
}

async function generate() {
    if (_busy || !_container) return;
    const $ = (id) => _container.querySelector(id);
    const prompt = $('#zis-prompt').value.trim();
    const statusEl = $('#zis-status');
    if (!prompt) { statusEl.textContent = 'Enter a prompt first.'; return; }

    const body = {
        prompt,
        count: parseInt($('#zis-count').value) || 1,
        steps: $('#zis-steps').value,
        cfg_scale: $('#zis-cfg').value,
        width: $('#zis-w').value,
        height: $('#zis-h').value,
        seed: $('#zis-seed').value.trim(),
        negative_prompt: $('#zis-neg').value,
        expand: $('#zis-expand').checked,
    };

    _busy = true;
    const btn = $('#zis-go');
    btn.disabled = true; btn.textContent = 'Generating…';
    statusEl.className = 'zis-status';
    statusEl.textContent = 'Generating…';

    try {
        const res = await fetch('/api/plugin/z-image/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF() },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            statusEl.className = 'zis-status err';
            statusEl.textContent = 'Failed: ' + (data.error || `HTTP ${res.status}`);
            return;
        }
        renderResults(data);
    } catch (e) {
        statusEl.className = 'zis-status err';
        statusEl.textContent = 'Request failed: ' + e.message;
    } finally {
        _busy = false;
        btn.disabled = false; btn.textContent = 'Generate';
    }
}

function renderResults(data) {
    const statusEl = _container.querySelector('#zis-status');
    const metaEl = _container.querySelector('#zis-meta');
    const imgsEl = _container.querySelector('#zis-imgs');
    const p = data.params || {};

    statusEl.className = 'zis-status';
    statusEl.textContent = `${data.images.length} image(s) in ${data.elapsed}s  ·  `
        + `${p.width}×${p.height}  steps ${p.steps}  cfg ${p.cfg_scale}`;

    metaEl.style.display = '';
    metaEl.textContent = `Prompt sent${p.expanded ? ' (expanded)' : ''}:\n${data.final_prompt}`
        + `\nSeeds: ${data.seeds.join(', ')}`;

    imgsEl.innerHTML = '';
    data.images.forEach((src, i) => {
        const fig = document.createElement('figure');
        const img = document.createElement('img');
        img.src = src; img.alt = `image ${i + 1}`;
        const cap = document.createElement('figcaption');
        const a = document.createElement('a');
        a.href = src; a.download = `zimage_${data.seeds[i]}.png`; a.textContent = 'download';
        cap.append(Object.assign(document.createElement('span'), { textContent: `#${i + 1} · seed ${data.seeds[i]}` }), a);
        fig.append(img, cap);
        imgsEl.appendChild(fig);
    });
}

export function cleanup() {
    _container = null;
    _busy = false;
}
