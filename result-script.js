/* ================== Result page logic (result.html only) ================== */

  // ---------- read everything app.html left in the browser "pocket" ----------
  const code   = sessionStorage.getItem('generatedCode');
  const isDemo = sessionStorage.getItem('isDemo') === 'yes';

  let formTask = null;
  try { formTask = JSON.parse(sessionStorage.getItem('payloadData')); } catch {}

  // the server's whole answer: plan, questions, checks (absent in demo mode)
  let plan = null;
  try { plan = JSON.parse(sessionStorage.getItem('planData')); } catch {}

  // same address as in app-script.js: answers to questions go there too
  const BACKEND_URL = 'https://staticas.onrender.com/generate';

  // yield strengths for the quick check, matched by name (MPa)
  const YIELD_MPA = [['6061', 275], ['1020', 350], ['304', 215],
                     ['ti-6al-4v', 880], ['titanium', 880], ['plain carbon', 220]];

  let task = null;
  let aiInterpreted = false;

  if (plan) {
    task = beamFromPlan(plan, formTask);
    aiInterpreted = true;
  } else {
    task = formTask;                    // demo mode: the form is all there is
  }

  const emptyState    = document.getElementById('emptyState');
  const resultContent = document.getElementById('resultContent');

  if (!code && !plan) {
    emptyState.hidden = false;
    resultContent.hidden = true;
  } else {
    document.getElementById('demoBadge').hidden = !isDemo;
    if (plan) renderPlan(plan);
    if (code) {
      document.getElementById('codeOutput').textContent = code;
      setupButtons();
    } else {
      document.getElementById('codeCard').hidden = true;
    }
    if (task) {
      renderSummary(task);
      renderSchematic(task);
      renderCheck(task);
    }
  }

  // text from the server goes on the page as text, never as markup
  function esc(s) {
    return String(s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  }

  function el(tag, text, cls) {
    const e = document.createElement(tag);
    if (text != null) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }

  // ---------- the schematic and quick check only fit one shape: a cantilever ----------
  // a rectangular beam, held at one end, one sideways force at the other. Anything
  // else gets the plan without a drawing rather than a drawing of the wrong thing.
  function beamFromPlan(p, form) {
    if (p.domain !== 'static' || !p.ready) return null;
    const steps = p.steps || [];
    const geom = steps.find(s => s.op === 'set_geometry' &&
                                 (s.options || {}).primitive === 'rectangular_beam');
    const fixtures = steps.filter(s => s.op === 'add_fixture');
    const forces = steps.filter(s => s.op === 'add_force');
    if (!geom || fixtures.length !== 1 || forces.length !== 1) return null;

    const at = (s) => {
      const t = s.target || {};
      if (t.role === 'fixed_end' || (t.kind === 'extreme' && t.side === 'min')) return 'min';
      if (t.role === 'free_end' || (t.kind === 'extreme' && t.side === 'max')) return 'max';
      return '';
    };
    const dir = String((forces[0].options || {}).direction || '');
    if (at(fixtures[0]) !== 'min' || at(forces[0]) !== 'max' || !/_(x|y)$/.test(dir)) return null;

    const q = geom.quantities || {};
    const num = (k) => (q[k] ? q[k].value : NaN);
    const mat = steps.find(s => s.op === 'apply_material');
    const name = mat ? (mat.options || {}).name || 'default material' : 'default material';
    const known = YIELD_MPA.find(([k]) => name.toLowerCase().includes(k));
    const formYield = form && form.material && form.material.yieldStrengthMPa;

    return {
      material: { name: name, yieldStrengthMPa: formYield > 0 ? formYield : (known ? known[1] : null) },
      geometry: { lengthM: num('length'), widthM: num('width'), heightM: num('height') },
      loads: { fixture: 'Fixed at left end', forceN: forces[0].quantities.magnitude.value },
      mesh: form ? form.mesh : 'coarse'
    };
  }

  // ---------- what the AI understood, the plan, the questions ----------
  function renderPlan(p) {
    document.getElementById('pageTitle').textContent =
      p.ready ? '✅ Your script is ready' : '📝 The plan needs a few details';

    document.getElementById('studyTag').textContent = p.domain_label || p.domain;
    document.getElementById('aiReply').textContent = p.reply || p.summary || '';
    document.getElementById('aiCard').hidden = false;

    const list = document.getElementById('planSteps');
    list.replaceChildren();
    for (const s of p.steps || []) {
      const li = el('li', s.note || s.label);
      const details = [];
      for (const [k, v] of Object.entries(s.quantities || {})) {
        const said = v.original && v.original !== 'default' ? ' (you wrote ' + v.original + ')' : '';
        details.push(k.replace(/_/g, ' ') + ' = ' + (+v.value.toPrecision(5)) + ' ' + (v.unit || '') + said);
      }
      for (const [k, v] of Object.entries(s.options || {})) details.push(k + ': ' + v);
      if (s.needs_target && s.target_text) details.push('on: ' + s.target_text);
      if (s.confidence === 'unverified') details.push('API call not yet verified on SolidWorks');
      if (details.length) li.append(el('div', details.join(' · '), 'step-detail'));
      list.append(li);
    }

    const as = p.assumptions || [];
    document.getElementById('assumptionsBox').hidden = !as.length;
    document.getElementById('assumptions').replaceChildren(...as.map(a => el('li', a)));

    const checks = document.getElementById('checks');
    checks.replaceChildren();
    const pf = p.preflight || {};
    for (const f of pf.findings || []) {
      const box = el('div', f.message + (f.detail ? ' — ' + f.detail : ''),
                     'verdict ' + (f.level === 'blocking' ? 'bad' : f.level === 'warning' ? 'warn' : 'ok'));
      checks.append(box);
    }
    for (const w of p.warnings || []) checks.append(el('div', w, 'verdict warn'));
    for (const g of p.gaps || []) checks.append(el('div', 'Missing: ' + g, 'verdict warn'));
    if (p.blocked_by) checks.append(el('div', p.blocked_by, 'verdict bad'));
    document.getElementById('planCard').hidden = false;

    const qs = p.questions || [];
    document.getElementById('questionsCard').hidden = !qs.length;
    const box = document.getElementById('questions');
    box.replaceChildren();
    for (const q of qs) {
      const div = el('div', null, 'question');
      div.append(el('div', q.question));
      if (q.why) div.append(el('div', q.why, 'why'));
      if (q.options && q.options.length) div.append(el('div', 'Options: ' + q.options.join(', '), 'why'));
      const input = el('input');
      input.type = 'text';
      input.placeholder = q.suggested || 'your answer';
      input.dataset.q = q.question;
      div.append(input);
      box.append(div);
    }
  }

  // answers go back in the same session, so the plan is edited, not rebuilt
  document.getElementById('answerBtn').addEventListener('click', async () => {
    const err = document.getElementById('answerError');
    const answers = [...document.querySelectorAll('#questions input')]
      .filter(i => i.value.trim()).map(i => i.dataset.q + ' ' + i.value.trim());
    if (!answers.length) { err.textContent = 'Answer at least one question.'; err.hidden = false; return; }

    const btn = document.getElementById('answerBtn');
    btn.disabled = true;
    btn.textContent = '⏳ Rebuilding the plan...';
    err.hidden = true;
    const fd = new FormData();
    fd.append('message', answers.join('  '));
    fd.append('session_id', (plan && plan.session_id) || '');
    try {
      const r = await fetch(BACKEND_URL, { method: 'POST', body: fd });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Server error: ' + r.status);
      sessionStorage.setItem('planData', JSON.stringify(data));
      sessionStorage.setItem('generatedCode', data.code || '');
      location.reload();
    } catch (e) {
      err.textContent = e.message || 'Could not reach the server.';
      err.hidden = false;
      btn.disabled = false;
      btn.textContent = 'Answer and rebuild the plan';
    }
  });

  // ---------- description of the part ----------
  function renderSummary(t) {
    const fixture = t.loads.fixture;
    const forceSpot = {
      'Fixed at left end':  'at the free right end',
      'Fixed at right end': 'at the free left end',
      'Fixed at both ends': 'at the middle of the span',
      'Simply supported':   'at the middle of the span',
      'Fixed at bottom':    'at the free top face (pulling up)'
    }[fixture] || 'on the beam';

    document.getElementById('partSummary').innerHTML =
      'A <b>' + esc(t.material.name) + '</b> beam, ' +
      '<b>' + esc(t.geometry.lengthM) + ' × ' + esc(t.geometry.widthM) + ' × ' + esc(t.geometry.heightM) + ' m</b> (L × W × H), ' +
      'supported as: <b>' + esc(fixture) + '</b>. ' +
      'A force of <b>' + esc(t.loads.forceN) + ' N</b> is applied ' + forceSpot + '. ' +
      'Mesh: <b>' + (t.mesh === 'coarse' ? 'extremely coarse (PC protection)' : 'standard') + '</b>.' +
      (aiInterpreted
        ? '<br><span style="font-size:12.5px;color:#182433;">These parameters are what the AI understood from your problem (text/photo) — check that the sketch below matches your task.</span>'
        : '');

    document.getElementById('partCard').hidden = false;
  }

  // ---------- schematic drawing (SVG built from the task data) ----------
  function renderSchematic(t) {
    const fixture = t.loads.fixture;

    // --- 1. VERTICAL SCHEMATIC (Axial Tension) ---
    if (fixture === 'Fixed at bottom') {
        document.getElementById('schematic').innerHTML = `
        <svg viewBox="0 0 640 320" xmlns="http://www.w3.org/2000/svg">
          <!-- hatched wall at the bottom -->
          <line x1="260" y1="250" x2="380" y2="250" stroke="#26334d" stroke-width="3"/>
          ${[0,1,2,3,4,5,6,7].map(i => `
            <line x1="${265 + i*15}" y1="250" x2="${255 + i*15}" y2="265" stroke="#26334d" stroke-width="1.5"/>
          `).join('')}
          
          <!-- vertical bar -->
          <rect x="296" y="70" width="48" height="180" fill="#c8d6ea" stroke="#26334d" stroke-width="2"/>
          
          <!-- force arrow pointing UP -->
          <line x1="320" y1="70" x2="320" y2="15" stroke="#d97706" stroke-width="3"/>
          <path d="M 320 10 L 314 22 L 326 22 Z" fill="#d97706"/>
          <text x="335" y="40" font-size="15" font-weight="bold" fill="#d97706" font-family="Consolas, monospace">F = ${t.loads.forceN} N</text>
          
          <!-- vertical dimension line -->
          <line x1="240" y1="70" x2="240" y2="250" stroke="#26334d" stroke-width="1"/>
          <line x1="232" y1="70" x2="248" y2="70" stroke="#26334d" stroke-width="1"/>
          <line x1="232" y1="250" x2="248" y2="250" stroke="#26334d" stroke-width="1"/>
          <text x="220" y="165" font-size="13" fill="#26334d" text-anchor="middle" transform="rotate(-90, 220, 165)" font-family="Consolas, monospace">L = ${t.geometry.lengthM} m</text>
          
          <!-- cross-section note -->
          <text x="320" y="295" font-size="12.5" fill="#182433" text-anchor="middle" font-family="Consolas, monospace">Cross-section W × H = ${t.geometry.widthM} × ${t.geometry.heightM} m</text>
        </svg>`;
        return; // exit early, no need to draw the horizontal one
    }

    // --- 2. HORIZONTAL SCHEMATIC (Bending) ---
    const fx = { 'Fixed at left end': 505, 'Fixed at right end': 135,
                 'Fixed at both ends': 320, 'Simply supported': 320 }[fixture] || 320;

    const wall = (x, flip) => `
      <line x1="${x}" y1="80" x2="${x}" y2="184" stroke="#26334d" stroke-width="3"/>
      ${[0,1,2,3,4,5,6].map(i => `
        <line x1="${x}" y1="${88 + i*14}" x2="${x + (flip ? 14 : -14)}" y2="${78 + i*14}" stroke="#26334d" stroke-width="1.5"/>
      `).join('')}`;

    const tri = (x) => `
      <path d="M ${x} 156 L ${x-14} 180 L ${x+14} 180 Z" fill="none" stroke="#26334d" stroke-width="2"/>
      <line x1="${x-20}" y1="180" x2="${x+20}" y2="180" stroke="#26334d" stroke-width="2"/>`;

    let supports = '';
    if (fixture === 'Fixed at left end')  supports = wall(118, false);
    if (fixture === 'Fixed at right end') supports = wall(522, true);
    if (fixture === 'Fixed at both ends') supports = wall(118, false) + wall(522, true);
    if (fixture === 'Simply supported')   supports = tri(140) + tri(500);

    document.getElementById('schematic').innerHTML = `
    <svg viewBox="0 0 640 250" xmlns="http://www.w3.org/2000/svg">
      <rect x="120" y="112" width="400" height="44" fill="#c8d6ea" stroke="#26334d" stroke-width="2"/>
      ${supports}
      <line x1="${fx}" y1="46" x2="${fx}" y2="104" stroke="#d97706" stroke-width="3"/>
      <path d="M ${fx} 110 L ${fx-6} 98 L ${fx+6} 98 Z" fill="#d97706"/>
      <text x="${fx + 10}" y="60" font-size="15" font-weight="bold" fill="#d97706" font-family="Consolas, monospace">F = ${t.loads.forceN} N</text>
      <line x1="120" y1="210" x2="520" y2="210" stroke="#26334d" stroke-width="1"/>
      <line x1="120" y1="202" x2="120" y2="218" stroke="#26334d" stroke-width="1"/>
      <line x1="520" y1="202" x2="520" y2="218" stroke="#26334d" stroke-width="1"/>
      <text x="320" y="232" font-size="13" fill="#26334d" text-anchor="middle" font-family="Consolas, monospace">L = ${t.geometry.lengthM} m</text>
      <text x="320" y="30" font-size="12.5" fill="#182433" text-anchor="middle" font-family="Consolas, monospace">Cross-section W × H = ${t.geometry.widthM} × ${t.geometry.heightM} m</text>
    </svg>`;
  }

  // ---------- quick strength estimate ----------
  function renderCheck(t) {
    const F = t.loads.forceN;                 
    const L = t.geometry.lengthM;             
    const b = t.geometry.widthM;              
    const h = t.geometry.heightM;             
    const yieldMPa = t.material.yieldStrengthMPa;
    const fixture = t.loads.fixture;

    let sigmaMPa = 0;
    let calcLabel = '';

    if (fixture === 'Fixed at bottom') {
        // Axial Tension formula: Stress = Force / Area
        const area = b * h; 
        sigmaMPa = (F / area) / 1e6; // Convert to MPa
        calcLabel = 'axial tensile stress (σ = F / A)';
    } else {
        // Bending formula: Stress = M * c / I
        const k = { 'Fixed at left end': 1, 'Fixed at right end': 1,
                    'Fixed at both ends': 1/8, 'Simply supported': 1/4 }[fixture] || 1;
        const M = k * F * L;
        sigmaMPa = (6 * M) / (b * h * h) / 1e6;
        calcLabel = 'max bending stress (σ = M·c / I)';
    }

    const FS = yieldMPa / sigmaMPa;

    const box = document.getElementById('verdictBox');
    const s = sigmaMPa.toFixed(1);
    const f = FS.toFixed(1);

    // a material the table does not know: the stress, without a verdict
    if (!(yieldMPa > 0)) {
      box.innerHTML = '<div class="verdict ok">Estimated ' + calcLabel + ' ≈ <b>' + s +
        ' MPa</b>. Yield strength of ' + esc(t.material.name) +
        ' is not in the quick-check table — compare it yourself.</div>';
      document.getElementById('checkCard').hidden = false;
      return;
    }

    if (FS >= 1.5) {
      box.innerHTML = '<div class="verdict ok">✅ The beam should hold. ' +
        'Estimated ' + calcLabel + ' ≈ <b>' + s + ' MPa</b> vs yield strength ' + yieldMPa +
        ' MPa — factor of safety ≈ <b>' + f + '</b>.</div>';
    } else if (FS >= 1) {
      box.innerHTML = '<div class="verdict warn">⚠️ Close to the limit. ' +
        'Estimated ' + calcLabel + ' ≈ <b>' + s + ' MPa</b> vs yield ' + yieldMPa +
        ' MPa — factor of safety only ≈ <b>' + f + '</b>. Consider a smaller force or a thicker beam.</div>';
    } else {
      box.innerHTML = '<div class="verdict bad">❌ Likely to fail: estimated ' + calcLabel + ' ≈ <b>' + s +
        ' MPa</b> exceeds the yield strength ' + yieldMPa +
        ' MPa (factor of safety ≈ <b>' + f + '</b>). Reduce the force or increase the cross-section.</div>';
    }

    document.getElementById('checkCard').hidden = false;
  }

  // ---------- copy & download buttons ----------
  function setupButtons() {
    document.getElementById('downloadBtn').addEventListener('click', () => {
      const blob = new Blob([code], { type: 'text/x-python' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = 'solidworks_simulation.py';
      link.click();
      URL.revokeObjectURL(link.href);
    });

    document.getElementById('copyBtn').addEventListener('click', async () => {
      const btn = document.getElementById('copyBtn');
      try {
        await navigator.clipboard.writeText(code);
        btn.textContent = '✓ Copied';
      } catch {
        btn.textContent = '✗ Copy failed';
      }
      setTimeout(() => { btn.textContent = '📋 Copy'; }, 1600);
    });
  }

  
