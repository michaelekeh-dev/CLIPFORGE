const CF = (() => {
  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = (t) => { t = Math.max(0, Math.round(t || 0)); return `${Math.floor(t/60)}:${String(t%60).padStart(2,'0')}`; };
  let toastT; const toast = (m) => { const t = $('#toast'); t.textContent = m; t.classList.add('show'); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('show'), 2600); };
  const api = async (url, opts = {}) => { const r = await fetch(url, opts); if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); } return r.json(); };
  const copy = async (text) => { try { await navigator.clipboard.writeText(text); toast('Copied'); } catch { toast('Could not copy'); } };

  function home() {
    const form = $('#newProject'), file = $('#file'), url = $('#url');
    file.addEventListener('change', () => { $('#fileName').textContent = file.files[0] ? `File: ${file.files[0].name}` : ''; if (file.files[0]) url.value = ''; });
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      if (!url.value.trim() && !file.files[0]) { toast('Paste a link or choose a file'); return; }
      const fd = new FormData(form);
      const xhr = new XMLHttpRequest();
      const bar = $('#uploadBar'); const btn = $('#startBtn');
      btn.disabled = true; btn.textContent = file.files[0] ? 'Uploading…' : 'Starting…';
      if (file.files[0]) { bar.classList.remove('hidden'); xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) bar.firstElementChild.style.width = (100 * ev.loaded / ev.total) + '%'; }; }
      xhr.open('POST', '/api/projects');
      xhr.onload = () => { if (xhr.status === 200) { location.href = '/project/' + JSON.parse(xhr.responseText).id; } else { btn.disabled = false; btn.textContent = 'Make clips'; toast('Could not start: ' + xhr.responseText.slice(0, 120)); } };
      xhr.onerror = () => { btn.disabled = false; btn.textContent = 'Make clips'; toast('Network error'); };
      xhr.send(fd);
    });
    // live status for running projects
    const refresh = async () => {
      const cards = [...document.querySelectorAll('.project[data-id]')];
      if (!cards.length) return;
      try {
        const d = await api('/api/projects');
        for (const p of d.projects) {
          const el = cards.find(c => c.dataset.id === p.id); if (!el) continue;
          const st = $('.status', el); st.className = 'status s-' + p.status; st.textContent = p.status === 'running' ? p.stage : p.status;
          const b = $('.bar > div', el); if (b) b.style.width = p.progress + '%';
          if (p.status === 'done' && b) b.parentElement.remove();
          const img = $('.thumb img', el); if (!img && p.thumb_url) $('.thumb', el).innerHTML = `<img src="${p.thumb_url}" alt="">`;
        }
        if (d.projects.some(p => p.status === 'running' || p.status === 'queued')) setTimeout(refresh, 2500);
      } catch {}
    };
    refresh();
  }

  function project(pid) {
    const clipsEl = $('#clips');
    let last = '';
    const stageKey = (s) => /Download/.test(s) ? 0 : /Transcrib/.test(s) ? 1 : /moment|facts/i.test(s) ? 2 : /Render/.test(s) ? 3 : -1;
    const render = (p) => {
      $('#ptitle').textContent = p.title;
      if (p.thumb_url && !$('#head .thumb img')) $('#head .thumb').innerHTML = `<img src="${p.thumb_url}" alt="">`;
      $('#pmeta').textContent = (p.duration ? Math.round(p.duration / 60) + ' min' : '') + (p.info && p.info.pick_method ? ' · picked by ' + p.info.pick_method.replace(/\(.*\)/, '').trim() : '');
      const running = p.status === 'running' || p.status === 'queued';
      $('#progress').classList.toggle('hidden', !running);
      $('#errorBox').classList.toggle('hidden', p.status !== 'error');
      if (p.status === 'error') $('#perror').textContent = p.error || 'Unknown error';
      if (running) {
        const k = stageKey(p.stage || '');
        document.querySelectorAll('.stage').forEach((el, i) => { el.classList.toggle('active', i === k); el.classList.toggle('done', i < k); });
        $('#pbar').style.width = Math.max(2, p.progress) + '%';
        $('#pstage').textContent = p.stage || 'Working…';
      }
      $('#clipsTitle').textContent = p.clips.length ? `Clips (${p.clips.length})` : (running ? 'Clips will appear here' : 'Clips');
      const key = JSON.stringify(p.clips.map(c => [c.id, c.status, c.progress, c.title]));
      if (key !== last) { last = key; clipsEl.innerHTML = p.clips.map(clipCard).join(''); bind(p); }
      else p.clips.forEach(c => { const b = $(`[data-clip="${c.id}"] .cbar > div`); if (b) b.style.width = c.progress + '%'; });
    };
    const clipCard = (c) => `
      <article class="card clip" data-clip="${c.id}">
        <div class="player" style="view-transition-name: clip-${c.id}">
          ${c.status === 'done' && c.video_url ? `<video src="${c.video_url}" poster="${c.thumb_url}" controls playsinline preload="metadata"></video>`
            : `${c.thumb_url ? `<img src="${c.thumb_url}" alt="">` : ''}<div class="rendering">${c.status === 'error' ? `<div>⚠️ ${esc(c.error)}</div><button class="btn secondary small rerender">Try again</button>` : `<div class="spinner"></div><div class="muted small">${esc(c.stage || 'Waiting to render')}</div><div class="bar cbar"><div style="width:${c.progress}%"></div></div>`}</div>`}
        </div>
        <div class="body">
          <div class="meta"><span class="score">${c.score}</span><button class="badge" title="Fact check">${c.badge} ${esc(c.fact_check.type || 'check')}</button><span>${fmt(c.duration)} · ${fmt(c.start)}–${fmt(c.end)}</span></div>
          <a class="title" href="/clip/${c.id}">${esc(c.title)}</a>
          <a class="btn secondary small edit-link" href="/clip/${c.id}">✎ Edit clip</a>
          <details><summary class="why">Why this clip</summary><ul class="why-list">${Object.entries(c.reasons || {}).map(([k, v]) => `<li><b>${esc(k.replace('_', ' '))}:</b> ${esc(v)}</li>`).join('')}</ul></details>
          <div class="tools">
            <select class="input style-select" title="Caption style">${styleOptions(c.settings.style || 'auto')}</select>
            <select class="input layout-select" title="Framing">${['auto','single','split','wide'].map(l => `<option value="${l}" ${(c.settings.layout || 'auto') === l ? 'selected' : ''}>${{auto:'Auto framing', single:'One person', split:'Two people', wide:'Whole picture'}[l]}</option>`).join('')}</select>
            <select class="input ratio-select" title="Shape">${['9:16','1:1','16:9'].map(r => `<option value="${r}" ${(c.settings.ratio || '9:16') === r ? 'selected' : ''}>${{'9:16':'Vertical 9:16','1:1':'Square 1:1','16:9':'Wide 16:9'}[r]}</option>`).join('')}</select>
            <select class="input template-select" title="Brand template">${(window.CF_TEMPLATE_IDS || []).map((id, i) => `<option value="${id}" ${(c.settings.template || '') === id || (!c.settings.template && i === 0) ? 'selected' : ''}>${esc(window.CF_TEMPLATES[i])}</option>`).join('')}</select>
            <select class="input filler-select" title="Filler removal">${['off','light','aggressive'].map(l => `<option value="${l}" ${(c.settings.filler || 'light') === l ? 'selected' : ''}>${{off:'Keep pauses', light:'Trim pauses: light', aggressive:'Trim pauses: aggressive'}[l]}</option>`).join('')}</select>
          </div>
          <div class="tools">
            <label class="check small"><input type="checkbox" class="emoji-check" ${c.settings.emoji === false ? '' : 'checked'}> Emoji</label>
            <label class="check small"><input type="checkbox" class="hook-check" ${c.settings.hook === false ? '' : 'checked'}> Hook</label>
            <label class="check small"><input type="checkbox" class="zooms-check" ${c.settings.zooms === false ? '' : 'checked'}> Zooms</label>
            <label class="check small"><input type="checkbox" class="bar-check" ${c.settings.progress_bar === false ? '' : 'checked'}> Bar</label>
          </div>
          <div class="hook-row"><input class="input hook-text" placeholder="Hook text (6-8 words)" value="${esc(c.settings.hook_text || (c.hook && c.hook.text) || '')}" maxlength="80"><button class="btn secondary small rerender-btn" ${c.status === 'rendering' ? 'disabled' : ''}>Re-render</button></div>
          <div class="actions">
            <a class="btn primary small" href="${c.download_url}" ${c.status === 'done' ? '' : 'aria-disabled="true" style="opacity:.5;pointer-events:none"'}>Download</a>
            <button class="btn secondary small copy">Copy title + tags</button>
            ${c.thumbnail_url ? `<a class="btn secondary small" href="${c.thumbnail_url}" title="Thumbnail">🖼</a>` : ''}
          </div>
        </div>
      </article>`;
    const styleOptions = (cur) => ['<option value="auto">Auto style</option>'].concat((window.CF_PRESETS || []).map(p => `<option value="${p.id}" ${p.id === cur ? 'selected' : ''}>${esc(p.label)}</option>`)).join('');
    const bind = (p) => {
      p.clips.forEach(c => {
        const el = $(`[data-clip="${c.id}"]`); if (!el) return;
        const rr2 = $('.rerender-btn', el); if (rr2) rr2.onclick = async () => {
          rr2.disabled = true;
          await api(`/api/clips/${c.id}/settings`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ style: $('.style-select', el).value, layout: $('.layout-select', el).value, emoji: $('.emoji-check', el).checked,
              filler: $('.filler-select', el).value, hook: $('.hook-check', el).checked, zooms: $('.zooms-check', el).checked,
              progress_bar: $('.bar-check', el).checked, hook_text: $('.hook-text', el).value.trim(),
              ratio: $('.ratio-select', el).value, template: $('.template-select', el).value, render: true }) });
          toast('Rendering again…'); poll();
        };
        $('.copy', el).onclick = () => copy(`${c.title}\n\n${c.description}\n\n${(c.hashtags || []).join(' ')}`);
        $('.badge', el).onclick = () => showFact(c);
        const rr = $('.rerender', el); if (rr) rr.onclick = () => api(`/api/clips/${c.id}/render`, { method: 'POST' }).then(poll);
      });
    };
    const showFact = (c) => {
      const f = c.fact_check || {};
      const pill = (s) => `<span class="pill ${esc((s || '').split(' ')[0])}">${esc(s)}</span>`;
      $('#modalBody').innerHTML = `<div class="fact">
        <h3>${c.badge} ${esc(f.verdict || 'not checked')} · ${esc(f.type || '')}</h3>
        <p>${esc(f.summary || '')}</p>
        ${f.claims && f.claims.length ? `<h4>Claims</h4><ul>${f.claims.map(x => `<li>${pill(x.status)} ${esc(x.claim)}${x.note ? ` <span class="muted">– ${esc(x.note)}</span>` : ''}</li>`).join('')}</ul>` : ''}
        ${f.scripture && f.scripture.length ? `<h4>Scripture</h4><ul>${f.scripture.map(x => `<li>${pill(x.in_context ? 'ok' : 'needs context')} ${esc(x.reference)} <span class="muted">– ${esc(x.note)}</span></li>`).join('')}</ul>` : ''}
        ${f.red_flags && f.red_flags.length ? `<h4>Watch out</h4><ul>${f.red_flags.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
        <p class="muted small">Original title: ${esc(c.original_title)}<br>Checked by: ${esc(f.checked_by || '')}</p>
      </div>`;
      $('#modal').classList.remove('hidden');
    };
    $('#modalClose').onclick = () => $('#modal').classList.add('hidden');
    $('#modal').onclick = (e) => { if (e.target.id === 'modal') $('#modal').classList.add('hidden'); };
    const cs = $('#creditSave'); if (cs) cs.onclick = async () => { await api(`/api/projects/${pid}/settings`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ credit_name: $('#creditName').value }) }); toast('Saved. Re-render a clip to apply.'); };
    $('#retryBtn').onclick = () => api(`/api/projects/${pid}/retry`, { method: 'POST' }).then(poll);
    $('#deleteBtn').onclick = async () => { if (!confirm('Delete this project and its clips?')) return; await api(`/api/projects/${pid}`, { method: 'DELETE' }); location.href = '/'; };
    let timer;
    const poll = async () => {
      clearTimeout(timer);
      try { const p = await api(`/api/projects/${pid}`); render(p);
        if (p.status === 'running' || p.status === 'queued' || p.clips.some(c => c.status === 'rendering' || c.status === 'queued' || c.status === 'pending' || c.status === 'running')) timer = setTimeout(poll, 1500);
      } catch (e) { timer = setTimeout(poll, 4000); }
    };
    poll();
  }
  function templates() {
    document.querySelectorAll('.tcard').forEach(card => {
      const id = card.dataset.id;
      const edit = $('.edit-btn', card), form = $('.tform', card);
      if (edit) edit.onclick = () => { form.classList.toggle('hidden'); };
      const cancel = $('.cancel-btn', card); if (cancel) cancel.onclick = () => form.classList.add('hidden');
      const md = $('.make-default', card); if (md) md.onclick = async () => { await api(`/api/templates/${id}/default`, { method: 'POST' }); location.reload(); };
      const del = $('.del-btn', card); if (del) del.onclick = async () => { if (!confirm('Delete this template?')) return; await api(`/api/templates/${id}`, { method: 'DELETE' }); location.reload(); };
    });
  }
  return { home, project, templates, toast, api, copy, esc, fmt };
})();
