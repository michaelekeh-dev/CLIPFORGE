(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const { api, toast, esc, fmt, copy } = CF;
  const cid = $('#editor').dataset.clip;
  let D = null;                 // editor data from the server
  let E = null;                 // edits {start,end,deleted,text_fixes,shot_layouts}
  let S = null;                 // settings
  const history = [];
  let selected = new Set();
  let pollTimer = null;
  const SETTING_KEYS = ['layout', 'ratio', 'style', 'filler', 'hook_text', 'hook', 'zooms', 'emoji', 'progress_bar', 'broll', 'credit', 'template'];

  const snapshot = () => JSON.stringify({ E, S });
  const pushHistory = () => { history.push(snapshot()); if (history.length > 50) history.shift(); $('#undoBtn').disabled = false; };
  const setStatus = (t) => { $('#estatus').textContent = t; };

  async function load() {
    D = await api(`/api/clips/${cid}/editor`);
    E = D.edits; S = Object.assign({}, D.settings);
    if (!('hook' in S)) S.hook = true; if (!('zooms' in S)) S.zooms = true; if (!('emoji' in S)) S.emoji = true;
    if (!('progress_bar' in S)) S.progress_bar = true; if (!('broll' in S)) S.broll = true; if (!('credit' in S)) S.credit = true;
    if (!S.hook_text && D.hook && D.hook.text) S.hook_text = D.hook.text;
    fillControls(); renderTranscript(); renderShots(); renderBroll();
    if (D.status === 'rendering' || D.status === 'queued' || D.status === 'running') startPolling();
  }

  // ---------------------------------------------------------------- transcript
  const inRange = (w) => w.s >= E.start - 0.05 && w.e <= E.end + 0.05;
  const isDeleted = (w) => E.deleted.some(([a, b]) => w.s >= a - 0.011 && w.e <= b + 0.011);
  function renderTranscript() {
    const box = $('#transcript'); box.innerHTML = '';
    let lastEnd = null, startPlaced = false, endPlaced = false;
    D.words.forEach((w, idx) => {
      if (lastEnd !== null && w.s - lastEnd > 1.2) box.appendChild(Object.assign(document.createElement('span'), { className: 'gap' }));
      if (!startPlaced && inRange(w)) { box.appendChild(handle('start')); startPlaced = true; }
      const el = document.createElement('span');
      const fixed = E.text_fixes[String(w.i)];
      el.className = 'w' + (inRange(w) ? ' in' : '') + (isDeleted(w) ? ' deleted' : '') + (selected.has(w.i) ? ' selected' : '') + (fixed !== undefined ? ' fixed' : '');
      el.textContent = fixed !== undefined ? (fixed || '∅') : w.w;
      el.dataset.i = w.i; el.dataset.s = w.s; el.dataset.e = w.e;
      el.title = fmt(w.s);
      box.appendChild(el); box.appendChild(document.createTextNode(' '));
      const next = D.words[idx + 1];
      if (startPlaced && !endPlaced && inRange(w) && (!next || !inRange(next))) { box.appendChild(handle('end')); endPlaced = true; }
      lastEnd = w.e;
    });
    const dur = E.end - E.start - E.deleted.reduce((a, [s, e]) => a + (e - s), 0);
    $('#rangeInfo').textContent = `Clip: ${fmt(E.start)} to ${fmt(E.end)} in the video · about ${fmt(dur)} long · ${E.deleted.length} bits cut out · ${Object.keys(E.text_fixes).length} words fixed`;
    $('#selActions').classList.toggle('hidden', selected.size === 0);
  }
  function handle(kind) {
    const h = document.createElement('span'); h.className = 'handle'; h.dataset.kind = kind; h.title = kind === 'start' ? 'Drag to move the start' : 'Drag to move the end';
    h.addEventListener('pointerdown', (ev) => {
      ev.preventDefault(); h.setPointerCapture(ev.pointerId);
      const before = snapshot();
      const move = (e2) => {
        const el = document.elementFromPoint(e2.clientX, e2.clientY);
        if (!el || !el.classList.contains('w')) return;
        const s = parseFloat(el.dataset.s), e = parseFloat(el.dataset.e);
        if (kind === 'start' && s < E.end - 2) E.start = Math.round((s - 0.15) * 1000) / 1000;
        if (kind === 'end' && e > E.start + 2) E.end = Math.round((e + 0.35) * 1000) / 1000;
        document.querySelectorAll('#transcript .w').forEach(x => { const ws = parseFloat(x.dataset.s), we = parseFloat(x.dataset.e); x.classList.toggle('in', ws >= E.start - 0.05 && we <= E.end + 0.05); });
      };
      const up = () => { h.removeEventListener('pointermove', move); h.removeEventListener('pointerup', up); h.removeEventListener('pointercancel', up); if (snapshot() !== before) { history.push(before); $('#undoBtn').disabled = false; } trimDeleted(); renderTranscript(); save(); };
      h.addEventListener('pointermove', move); h.addEventListener('pointerup', up); h.addEventListener('pointercancel', up);
    });
    return h;
  }
  const trimDeleted = () => { E.deleted = E.deleted.filter(([a, b]) => a >= E.start && b <= E.end); };

  // tap = select, hold = fix text, tap while playing = seek
  let pressTimer = null, pressed = null;
  $('#transcript').addEventListener('pointerdown', (ev) => {
    const el = ev.target.closest('.w'); if (!el) return;
    pressed = el; pressTimer = setTimeout(() => { pressTimer = null; fixWord(parseInt(el.dataset.i)); }, 550);
  });
  const cancelPress = () => { if (pressTimer) clearTimeout(pressTimer); pressTimer = null; };
  $('#transcript').addEventListener('pointermove', (ev) => { if (pressed && ev.target !== pressed) cancelPress(); });
  $('#transcript').addEventListener('pointerup', (ev) => {
    const el = ev.target.closest('.w');
    if (pressTimer && el && el === pressed) { cancelPress(); const i = parseInt(el.dataset.i); if (selected.has(i)) selected.delete(i); else selected.add(i); seekTo(parseFloat(el.dataset.s)); renderTranscript(); }
    cancelPress(); pressed = null;
  });
  $('#transcript').addEventListener('pointercancel', () => { cancelPress(); pressed = null; });

  function fixWord(i) {
    const w = D.words.find(x => x.i === i); if (!w) return;
    const cur = E.text_fixes[String(i)] !== undefined ? E.text_fixes[String(i)] : w.w;
    const t = prompt('Fix this word (empty removes it from the captions):', cur);
    if (t === null) return;
    pushHistory();
    if (t.trim() === w.w) delete E.text_fixes[String(i)]; else E.text_fixes[String(i)] = t.trim();
    renderTranscript(); save();
  }

  function seekTo(srcT) {
    const v = $('#video'); if (!v || !D.timeline) return;
    let acc = D.lead_in || 0;
    for (const [a, b] of D.timeline) { if (srcT >= a && srcT <= b) { v.currentTime = acc + (srcT - a); return; } acc += b - a; }
  }
  const selWords = () => D.words.filter(w => selected.has(w.i));
  $('#delBtn').onclick = () => {
    const ws = selWords(); if (!ws.length) return; pushHistory();
    const byI = Object.fromEntries(D.words.map(w => [w.i, w]));
    ws.forEach(w => { let s = w.s - 0.02, e = w.e + 0.02; const p = byI[w.i - 1], n = byI[w.i + 1]; if (p) s = Math.max(s, p.e + 0.01); if (n) e = Math.min(e, n.s - 0.01); if (e > s) E.deleted.push([Math.round(s * 1000) / 1000, Math.round(e * 1000) / 1000]); });
    E.deleted.sort((a, b) => a[0] - b[0]); selected.clear(); renderTranscript(); save();
  };
  $('#restoreBtn').onclick = () => { const ws = selWords(); if (!ws.length) return; pushHistory(); E.deleted = E.deleted.filter(([a, b]) => !ws.some(w => w.s >= a - 0.011 && w.e <= b + 0.011)); selected.clear(); renderTranscript(); save(); };
  $('#startHereBtn').onclick = () => { const ws = selWords(); if (!ws.length) return; pushHistory(); E.start = Math.round((Math.min(...ws.map(w => w.s)) - 0.15) * 1000) / 1000; if (E.end - E.start < 2) E.end = E.start + 5; trimDeleted(); selected.clear(); renderTranscript(); save(); };
  $('#endHereBtn').onclick = () => { const ws = selWords(); if (!ws.length) return; pushHistory(); E.end = Math.round((Math.max(...ws.map(w => w.e)) + 0.35) * 1000) / 1000; if (E.end - E.start < 2) E.start = Math.max(0, E.end - 5); trimDeleted(); selected.clear(); renderTranscript(); save(); };
  $('#fixBtn').onclick = () => { const ws = selWords(); if (ws.length) fixWord(ws[0].i); };
  $('#clearSelBtn').onclick = () => { selected.clear(); renderTranscript(); };

  // ---------------------------------------------------------------- controls
  function fillControls() {
    $('#c-style').innerHTML = ['<option value="auto">Auto</option>'].concat((window.CF_PRESETS || []).map(p => `<option value="${p.id}">${esc(p.label)}</option>`)).join('');
    $('#c-template').innerHTML = (window.CF_TEMPLATE_IDS || []).map((id, i) => `<option value="${id}">${esc(window.CF_TEMPLATES[i])}</option>`).join('');
    for (const k of SETTING_KEYS) {
      const el = $('#c-' + k); if (!el) continue;
      if (el.type === 'checkbox') el.checked = S[k] !== false; else el.value = S[k] || (k === 'template' ? (window.CF_TEMPLATE_IDS || [''])[0] : (k === 'filler' ? 'light' : k === 'ratio' ? '9:16' : 'auto'));
      el.onchange = () => { pushHistory(); S[k] = el.type === 'checkbox' ? el.checked : el.value; save(); };
    }
    $('#c-hook_text').oninput = () => { S.hook_text = $('#c-hook_text').value; };
    $('#c-hook_text').onchange = () => { pushHistory(); save(); };
  }
  function renderShots() {
    const box = $('#shots'); box.innerHTML = '';
    if (!D.shots || !D.shots.length) { box.innerHTML = '<p class="muted small">Camera shots appear here after the first render.</p>'; return; }
    D.shots.forEach(sh => {
      const key = String(sh.start);
      const row = document.createElement('div'); row.className = 'shot';
      row.innerHTML = `<span>${fmt(sh.start)}–${fmt(sh.end)} · ${sh.faces} face${sh.faces === 1 ? '' : 's'} · ${esc(sh.layout)}</span><select class="input">${['auto', 'single', 'split', 'wide'].map(l => `<option value="${l}" ${(E.shot_layouts[key] || 'auto') === l ? 'selected' : ''}>${{ auto: 'Auto', single: 'One person', split: 'Two people', wide: 'Whole picture' }[l]}</option>`).join('')}</select>`;
      $('select', row).onchange = (ev) => { pushHistory(); if (ev.target.value === 'auto') delete E.shot_layouts[key]; else E.shot_layouts[key] = ev.target.value; save(); };
      box.appendChild(row);
    });
  }
  function renderBroll() {
    const box = $('#brollList'); box.innerHTML = '';
    const items = (D.render && D.render.broll && D.render.broll.items) || [];
    if (!items.length) { box.innerHTML = '<p class="muted small">B-roll shots appear here after a render when the clip mentions something visual.</p>'; return; }
    const overrides = S.broll_items || {};
    items.forEach((it, idx) => {
      const ov = overrides[String(idx)] || {};
      const row = document.createElement('div'); row.className = 'broll-item';
      row.innerHTML = `${it.thumb ? `<img src="${it.thumb}" alt="">` : ''}<span>${fmt(it.t)} · </span><input class="input" value="${esc(ov.query !== undefined ? ov.query : it.query)}" placeholder="search words"><label class="check small"><input type="checkbox" ${ov.removed ? '' : 'checked'}> keep</label>`;
      const inp = $('input.input', row), chk = $('input[type=checkbox]', row);
      inp.onchange = () => { pushHistory(); S.broll_items = S.broll_items || {}; S.broll_items[String(idx)] = { ...(S.broll_items[String(idx)] || {}), query: inp.value.trim() }; save(); };
      chk.onchange = () => { pushHistory(); S.broll_items = S.broll_items || {}; S.broll_items[String(idx)] = { ...(S.broll_items[String(idx)] || {}), removed: !chk.checked }; save(); };
      box.appendChild(row);
    });
  }

  // ---------------------------------------------------------------- save / render / undo
  let saveTimer = null;
  function save(render = false) {
    clearTimeout(saveTimer);
    const doSave = async () => {
      try { await api(`/api/clips/${cid}/edits`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ edits: E, settings: S, render }) }); setStatus(render ? 'Rendering…' : 'Saved'); }
      catch (e) { toast('Could not save: ' + e.message); }
    };
    if (render) return doSave();
    saveTimer = setTimeout(doSave, 350);
  }
  $('#renderBtn').onclick = async () => { $('#renderBtn').disabled = true; await save(true); showRendering(true, 'Starting…', 0); startPolling(); };
  $('#undoBtn').onclick = () => { const prev = history.pop(); if (!prev) return; const o = JSON.parse(prev); E = o.E; S = o.S; $('#undoBtn').disabled = history.length === 0; fillControls(); renderTranscript(); renderShots(); renderBroll(); save(); toast('Undone'); };
  $('#resetBtn').onclick = async () => { if (!confirm('Throw away all edits on this clip?')) return; await api(`/api/clips/${cid}/reset`, { method: 'POST' }); location.reload(); };
  $('#copyBtn').onclick = () => copy(`${D.title}\n\n${D.description}\n\n${(D.hashtags || []).join(' ')}`);
  function showRendering(on, stage, pct) { $('#renderOverlay').classList.toggle('hidden', !on); if (on) { $('#renderStage').textContent = stage || 'Rendering…'; $('#renderBar').style.width = Math.max(2, pct || 0) + '%'; } }
  function startPolling() {
    clearTimeout(pollTimer);
    const tick = async () => {
      try {
        const p = await api(`/api/projects/${D.project_id}`);
        const c = p.clips.find(x => x.id === cid); if (!c) return;
        if (c.status === 'rendering' || c.status === 'queued' || c.status === 'running' || c.status === 'pending') { showRendering(true, c.stage, c.progress); pollTimer = setTimeout(tick, 1200); return; }
        showRendering(false); $('#renderBtn').disabled = false;
        if (c.status === 'error') { toast('Render failed: ' + c.error); setStatus('Failed'); return; }
        setStatus('Rendered'); toast('Clip updated');
        const v = $('#video');
        if (v) { const t = v.currentTime; v.src = c.video_url + '?t=' + Date.now(); v.load(); v.currentTime = Math.min(t, 1); } else { location.reload(); return; }
        const fresh = await api(`/api/clips/${cid}/editor`); D.timeline = fresh.timeline; D.shots = fresh.shots; D.render = fresh.render; D.lead_in = fresh.lead_in; renderShots(); renderBroll();
      } catch (e) { pollTimer = setTimeout(tick, 2500); }
    };
    tick();
  }
  // highlight the word being spoken
  setInterval(() => {
    const v = $('#video'); if (!v || v.paused || !D || !D.timeline) return;
    let t = v.currentTime - (D.lead_in || 0); let src = null;
    for (const [a, b] of D.timeline) { if (t <= b - a) { src = a + t; break; } t -= b - a; }
    document.querySelectorAll('#transcript .w.playing').forEach(x => x.classList.remove('playing'));
    if (src === null) return;
    const el = [...document.querySelectorAll('#transcript .w')].find(x => src >= parseFloat(x.dataset.s) && src < parseFloat(x.dataset.e) + 0.05);
    if (el) el.classList.add('playing');
  }, 200);

  load().catch(e => toast('Could not load the clip: ' + e.message));
})();
