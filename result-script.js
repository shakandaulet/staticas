/* ================== Result page logic (result.html only) ================== */

  // ---------- read everything app.html left in the browser "pocket" ----------
  const code   = sessionStorage.getItem('generatedCode');
  const isDemo = sessionStorage.getItem('isDemo') === 'yes';

  let formTask = null;
  try { formTask = JSON.parse(sessionStorage.getItem('payloadData')); } catch {}

  // parameters Gemini extracted from the user's text/photo (may be absent)
  let aiParsed = null;
  try { aiParsed = JSON.parse(sessionStorage.getItem('parsedData')); } catch {}

  // If the AI sent its own understanding of the task, the picture, the
  // description and the strength check are built from THAT — so they match
  // the user's photo, not just the form fields. Missing values fall back
  // to the form.
  const FIXTURES = ['Fixed at left end', 'Fixed at right end',
                    'Fixed at both ends', 'Simply supported'];

  function pickNum(aiValue, formValue) {
    const n = parseFloat(aiValue);
    return isNaN(n) || n <= 0 ? formValue : n;
  }

  let task = formTask;
  let aiInterpreted = false;

  if (aiParsed && formTask) {
    task = {
      material: {
        name: aiParsed.materialName || formTask.material.name,
        yieldStrengthMPa: pickNum(aiParsed.yieldStrengthMPa, formTask.material.yieldStrengthMPa)
      },
      geometry: {
        lengthM: pickNum(aiParsed.lengthM, formTask.geometry.lengthM),
        widthM:  pickNum(aiParsed.widthM,  formTask.geometry.widthM),
        heightM: pickNum(aiParsed.heightM, formTask.geometry.heightM)
      },
      loads: {
        fixture: FIXTURES.includes(aiParsed.fixture) ? aiParsed.fixture : formTask.loads.fixture,
        forceN:  pickNum(aiParsed.forceN, formTask.loads.forceN)
      },
      mesh: formTask.mesh
    };
    aiInterpreted = true;
  }

  const emptyState    = document.getElementById('emptyState');
  const resultContent = document.getElementById('resultContent');

  if (!code) {
    emptyState.hidden = false;
    resultContent.hidden = true;
  } else {
    document.getElementById('codeOutput').textContent = code;
    document.getElementById('demoBadge').hidden = !isDemo;
    setupButtons();
    if (task) {
      renderSummary(task);
      renderSchematic(task);
      renderCheck(task);
    }
  }

  // ---------- description of the part ----------
  function renderSummary(t) {
    const fixture = t.loads.fixture;
    const forceSpot = {
      'Fixed at left end':  'at the free right end',
      'Fixed at right end': 'at the free left end',
      'Fixed at both ends': 'at the middle of the span',
      'Simply supported':   'at the middle of the span'
    }[fixture] || 'on the beam';

    document.getElementById('partSummary').innerHTML =
      'A <b>' + t.material.name + '</b> beam, ' +
      '<b>' + t.geometry.lengthM + ' × ' + t.geometry.widthM + ' × ' + t.geometry.heightM + ' m</b> (L × W × H), ' +
      'supported as: <b>' + fixture + '</b>. ' +
      'A downward force of <b>' + t.loads.forceN + ' N</b> is applied ' + forceSpot + '. ' +
      'Mesh: <b>' + (t.mesh === 'coarse' ? 'extremely coarse (PC protection)' : 'standard') + '</b>.' +
      (aiInterpreted
        ? '<br><span style="font-size:12.5px;color:#182433;">These parameters are what the AI understood from your problem (text/photo) — check that the sketch below matches your task.</span>'
        : '');

    document.getElementById('partCard').hidden = false;
  }

  // ---------- schematic drawing (SVG built from the task data) ----------
  function renderSchematic(t) {
    const fixture = t.loads.fixture;

    // where the force arrow stands
    const fx = { 'Fixed at left end': 505, 'Fixed at right end': 135,
                 'Fixed at both ends': 320, 'Simply supported': 320 }[fixture] || 320;

    // hatched wall symbol
    const wall = (x, flip) => `
      <line x1="${x}" y1="80" x2="${x}" y2="184" stroke="#26334d" stroke-width="3"/>
      ${[0,1,2,3,4,5,6].map(i => `
        <line x1="${x}" y1="${88 + i*14}" x2="${x + (flip ? 14 : -14)}" y2="${78 + i*14}"
              stroke="#26334d" stroke-width="1.5"/>`).join('')}`;

    // triangle support symbol
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
      <!-- beam -->
      <rect x="120" y="112" width="400" height="44" fill="#c8d6ea" stroke="#26334d" stroke-width="2"/>
      ${supports}
      <!-- force arrow -->
      <line x1="${fx}" y1="46" x2="${fx}" y2="104" stroke="#d97706" stroke-width="3"/>
      <path d="M ${fx} 110 L ${fx-6} 98 L ${fx+6} 98 Z" fill="#d97706"/>
      <text x="${fx + 10}" y="60" font-size="15" font-weight="bold" fill="#d97706"
            font-family="Consolas, monospace">F = ${t.loads.forceN} N</text>
      <!-- dimension line -->
      <line x1="120" y1="210" x2="520" y2="210" stroke="#26334d" stroke-width="1"/>
      <line x1="120" y1="202" x2="120" y2="218" stroke="#26334d" stroke-width="1"/>
      <line x1="520" y1="202" x2="520" y2="218" stroke="#26334d" stroke-width="1"/>
      <text x="320" y="232" font-size="13" fill="#26334d" text-anchor="middle"
            font-family="Consolas, monospace">L = ${t.geometry.lengthM} m</text>
      <!-- cross-section note -->
      <text x="320" y="30" font-size="12.5" fill="#182433" text-anchor="middle"
            font-family="Consolas, monospace">Cross-section W × H = ${t.geometry.widthM} × ${t.geometry.heightM} m</text>
    </svg>`;
  }

  // ---------- quick strength estimate (classic beam bending) ----------
  function renderCheck(t) {
    const F = t.loads.forceN;                 // N
    const L = t.geometry.lengthM;             // m
    const b = t.geometry.widthM;              // m
    const h = t.geometry.heightM;             // m
    const yieldMPa = t.material.yieldStrengthMPa;

    // max bending moment depends on the support type:
    // cantilever: M = F·L | simply supported (center load): M = F·L/4 | fixed both ends: M = F·L/8
    const k = { 'Fixed at left end': 1, 'Fixed at right end': 1,
                'Fixed at both ends': 1/8, 'Simply supported': 1/4 }[t.loads.fixture] || 1;

    const M = k * F * L;                      // N·m
    const sigmaMPa = (6 * M) / (b * h * h) / 1e6;   // σ = 6M / (b·h²), in MPa
    const FS = yieldMPa / sigmaMPa;           // factor of safety

    const box = document.getElementById('verdictBox');
    const s = sigmaMPa.toFixed(1);
    const f = FS.toFixed(1);

    if (FS >= 1.5) {
      box.innerHTML = '<div class="verdict ok">✅ The beam should hold. ' +
        'Estimated max bending stress ≈ <b>' + s + ' MPa</b> vs yield strength ' + yieldMPa +
        ' MPa — factor of safety ≈ <b>' + f + '</b>.</div>';
    } else if (FS >= 1) {
      box.innerHTML = '<div class="verdict warn">⚠️ Close to the limit. ' +
        'Estimated max stress ≈ <b>' + s + ' MPa</b> vs yield ' + yieldMPa +
        ' MPa — factor of safety only ≈ <b>' + f + '</b>. Consider a smaller force or a thicker beam.</div>';
    } else {
      box.innerHTML = '<div class="verdict bad">❌ Likely to fail: estimated stress ≈ <b>' + s +
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

  
