/* ================== AutoStatics AI — Logic ================== */

// ================== LOGIN (Firebase) ==================
// Your Firebase "credentials" from https://console.firebase.google.com
const FIREBASE_CONFIG = {
  apiKey: "AIzaSy" + "AgCNbn3mntC6Xx9vNkF6cAU1puVhvl2IE",
  authDomain: "autostatistics-ai.firebaseapp.com",
  projectId: "autostatistics-ai",
  storageBucket: "autostatistics-ai.firebasestorage.app",
  messagingSenderId: "33573216130",
  appId: "1:33573216130:web:c59be676921faa25a3f99b"
};

const authOverlay    = document.getElementById('authOverlay');
const authEmail      = document.getElementById('authEmail');
const authPassword   = document.getElementById('authPassword');
const authError      = document.getElementById('authError');
const authSubmitBtn  = document.getElementById('authSubmitBtn');
const authSwitchLink = document.getElementById('authSwitchLink');
const authSwitchText = document.getElementById('authSwitchText');
const authSubtitle   = document.getElementById('authSubtitle');
const logoutBtn      = document.getElementById('logoutBtn');

let signUpMode = false; // false = Log In, true = Sign Up

// Show / hide password (the eye button)
const togglePassword = document.getElementById('togglePassword');
togglePassword.addEventListener('click', () => {
  const hidden = authPassword.type === 'password';
  authPassword.type = hidden ? 'text' : 'password';
  togglePassword.classList.toggle('active', hidden);
});

// Switch between "Log In" and "Sign Up"
authSwitchLink.addEventListener('click', (e) => {
  e.preventDefault();
  signUpMode = !signUpMode;
  authSubmitBtn.textContent  = signUpMode ? 'Sign Up' : 'Log In';
  authSubtitle.textContent   = signUpMode ? 'Create an account' : 'Log in to continue';
  authSwitchText.textContent = signUpMode ? 'Already have an account?' : 'No account yet?';
  authSwitchLink.textContent = signUpMode ? 'Log In' : 'Sign Up';
  authError.textContent = '';
});

if (FIREBASE_CONFIG) {
  // --- Real login through Firebase ---
  firebase.initializeApp(FIREBASE_CONFIG);
  const auth = firebase.auth();

  auth.onAuthStateChanged((user) => {
    authOverlay.hidden = !!user;
    logoutBtn.hidden = !user;
  });

  authSubmitBtn.addEventListener('click', async () => {
    const email = authEmail.value.trim();
    const password = authPassword.value;
    authError.textContent = '';
    try {
      if (signUpMode) {
        await auth.createUserWithEmailAndPassword(email, password);
      } else {
        await auth.signInWithEmailAndPassword(email, password);
      }
    } catch (err) {
      authError.textContent = friendlyAuthError(err.code);
    }
  });

  logoutBtn.addEventListener('click', () => auth.signOut());
} else {
  document.getElementById('authNote').hidden = false;
  authSubmitBtn.addEventListener('click', () => {
    authError.textContent = 'Firebase is not configured yet — use demo mode below.';
  });
  document.getElementById('demoLink').addEventListener('click', (e) => {
    e.preventDefault();
    authOverlay.hidden = true;
  });
}

// Translate Firebase error codes into human language
function friendlyAuthError(code) {
  switch (code) {
    case 'auth/invalid-email':        return 'Please enter a valid email address.';
    case 'auth/missing-password':
    case 'auth/weak-password':        return 'Password must be at least 6 characters.';
    case 'auth/email-already-in-use': return 'This email is already registered. Try logging in.';
    case 'auth/invalid-credential':
    case 'auth/wrong-password':
    case 'auth/user-not-found':       return 'Wrong email or password.';
    default:                          return 'Something went wrong. Please try again.';
  }
}

// ================== MAIN APP ==================

// ---- Material presets auto-fill ----
const materialSelect = document.getElementById('material');
materialSelect.addEventListener('change', () => {
  const opt = materialSelect.selectedOptions[0];
  if (opt.value !== 'custom') {
    document.getElementById('young').value = opt.dataset.e;
    document.getElementById('poisson').value = opt.dataset.nu;
    document.getElementById('yield').value = opt.dataset.sy;
  }
});

// ---- Image upload (click + drag & drop) with checks ----
const dropzone   = document.getElementById('dropzone');
const fileInput  = document.getElementById('fileInput');
const dzFile     = document.getElementById('dz-file');
const dzText     = document.getElementById('dz-text');
const imageError = document.getElementById('imageError');

const MAX_IMAGE_MB = 10;

dropzone.addEventListener('click', () => fileInput.click());

fileInput.addEventListener('change', () => {
  if (fileInput.files.length) acceptFile(fileInput.files[0]);
});

['dragover', 'dragenter'].forEach(ev =>
  dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.add('dragover'); })
);
['dragleave', 'drop'].forEach(ev =>
  dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.remove('dragover'); })
);
dropzone.addEventListener('drop', e => {
  if (e.dataTransfer.files.length) {
    fileInput.files = e.dataTransfer.files;
    acceptFile(e.dataTransfer.files[0]);
  }
});

// checks the file BEFORE accepting it
function acceptFile(file) {
  imageError.textContent = '';

  if (!file.type.startsWith('image/')) {
    clearFile();
    imageError.textContent = 'Only image files (JPG, PNG) are accepted.';
    return;
  }
  if (file.size > MAX_IMAGE_MB * 1024 * 1024) {
    clearFile();
    imageError.textContent = 'Image is too large — maximum ' + MAX_IMAGE_MB + ' MB.';
    return;
  }
  dzText.textContent = 'Image attached:';
  dzFile.textContent = file.name;
}

function clearFile() {
  fileInput.value = '';
  dzText.textContent = 'Click or drag a photo of the problem here';
  dzFile.textContent = '';
}

// ---- Number parsing that understands both 0.29 and 0,29 ----
function parseNum(id) {
  const raw = String(document.getElementById(id).value).replace(',', '.').trim();
  return parseFloat(raw);
}

// ---- Validation: returns a list of problems, highlights bad fields ----
function validateForm() {
  const problems = [];
  document.querySelectorAll('.invalid').forEach(el => el.classList.remove('invalid'));

  const mark = (id, msg) => {
    document.getElementById(id).classList.add('invalid');
    problems.push(msg);
  };

  // problem statement: need text OR image
  const hasText  = document.getElementById('prompt').value.trim().length > 0;
  const hasImage = fileInput.files.length > 0;
  if (!hasText && !hasImage) {
    mark('prompt', 'Describe the problem or upload a photo — at least one is required.');
  }

  const young = parseNum('young');
  if (isNaN(young) || young <= 0) mark('young', "Young's Modulus must be a positive number (GPa).");

  const nu = parseNum('poisson');
  if (isNaN(nu) || nu <= 0 || nu >= 0.5) mark('poisson', "Poisson's Ratio must be between 0 and 0.5 — physics does not allow more.");

  const sy = parseNum('yield');
  if (isNaN(sy) || sy <= 0) mark('yield', 'Yield Strength must be a positive number (MPa).');

  ['len', 'wid', 'hgt'].forEach(id => {
    const v = parseNum(id);
    const names = { len: 'Length', wid: 'Width', hgt: 'Height' };
    if (isNaN(v) || v <= 0) mark(id, names[id] + ' must be a positive number (m).');
  });

  const force = parseNum('force');
  if (isNaN(force) || force <= 0) mark('force', 'Force must be a positive number (N).');

  return problems;
}

function showErrors(problems) {
  const box = document.getElementById('formErrors');
  if (!problems.length) { box.hidden = true; return; }
  box.innerHTML = '<b>Please fix the following:</b><ul>' +
    problems.map(p => '<li>' + p + '</li>').join('') + '</ul>';
  box.hidden = false;
  box.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// ---- Backend address (API Contract with Student 2) ----
// Leave empty ('') while the backend is not ready — the button will
// then open the result page with a demo script.
// When Student 2's server is running, put its address here, e.g.:
// const BACKEND_URL = 'http://localhost:5000/api/generate';
const BACKEND_URL = '[https://staticas.onrender.com/generate](https://staticas.onrender.com/generate)';

// ---- Demo script shown while the backend is not connected ----
const DEMO_SCRIPT = `# ============================================================
#  DEMO SCRIPT
#  The backend is not connected yet: this is a realistic sample
#  of what the generated code will look like.
# ============================================================
import win32com.client

# 1. Connect to the running SolidWorks 2024 session
swApp = win32com.client.Dispatch("SldWorks.Application")
swApp.Visible = True

# 2. Load the Simulation add-in
swApp.LoadAddIn("SldWorks.Simulation")

# 3. Create a new part
model = swApp.NewPart()
model = swApp.ActiveDoc

# 4. Sketch the beam cross-section (W x H) on the Front plane
model.SketchManager.InsertSketch(True)
model.SketchManager.CreateCenterRectangle(0, 0, 0, 0.025, 0.05, 0)
model.SketchManager.InsertSketch(True)

# 5. Extrude the sketch to the beam length L = 2.0 m
model.FeatureManager.FeatureExtrusion2(
    True, False, False, 0, 0, 2.0, 0,
    False, False, False, False, 0, 0,
    False, False, False, False, True, True, True, 0, 0, False)

# 6. Apply material: AISI 1020 Steel
model.SetMaterialPropertyName2("", "SOLIDWORKS Materials", "AISI 1020 Steel")

# 7. Simulation study: fixture at the left end, 500 N at the right end
cw = swApp.GetAddInObject("CosmosWorks.CosmosWorks")
doc = cw.ActiveDoc()
study = doc.StudyManager.CreateNewStudy3("Static-1", 0, 0)

# ... fixture and load setup ...

# 8. COARSE MESH to protect the PC (large global element size)
mesh = study.Mesh
mesh.MesherType = 0
study.CreateMesh(0, 0.05, 0.01)

# 9. Run and save results
study.RunAnalysis()
model.Save()
print("Simulation complete - open the Simulation tab in SolidWorks")
`;

// puts the code AND the task data into the browser "pocket", opens the result page
// parsed = parameters extracted by Gemini (null if the server sent plain text)
function goToResult(code, isDemo, payload, parsed) {
  sessionStorage.setItem('generatedCode', code);
  sessionStorage.setItem('isDemo', isDemo ? 'yes' : 'no');
  sessionStorage.setItem('payloadData', JSON.stringify(payload));
  if (parsed) {
    sessionStorage.setItem('parsedData', JSON.stringify(parsed));
  } else {
    sessionStorage.removeItem('parsedData');
  }
  window.location.href = 'result.html';
}

// ---- Generate button ----
document.getElementById('generateBtn').addEventListener('click', async () => {

  // step 1: validate; stop if there are problems
  const problems = validateForm();
  showErrors(problems);
  if (problems.length) return;

  // step 2: collect the payload
  const payload = {
    prompt: document.getElementById('prompt').value,
    material: {
      name: materialSelect.selectedOptions[0].textContent,
      youngsModulusGPa: parseNum('young'),
      poissonsRatio: parseNum('poisson'),
      yieldStrengthMPa: parseNum('yield')
    },
    geometry: {
      lengthM: parseNum('len'),
      widthM: parseNum('wid'),
      heightM: parseNum('hgt')
    },
    loads: {
      fixture: document.getElementById('fixture').value,
      forceN: parseNum('force')
    },
    mesh: document.querySelector('input[name="mesh"]:checked').value
  };

  // step 3a: demo mode — open the result page with the sample script
  if (!BACKEND_URL) {
    console.log('Payload for backend:', payload);
    goToResult(DEMO_SCRIPT, true, payload, null);
    return;
  }

  // step 3b: real mode — send to the backend, then open the result page
  const formData = new FormData();
  formData.append('data', JSON.stringify(payload));   // field "data": the JSON
  if (fileInput.files.length) {
    formData.append('image', fileInput.files[0]);     // field "image": the picture (optional)
  }

  const btn = document.getElementById('generateBtn');
  btn.disabled = true;
  btn.textContent = '⏳ Generating script...';

  try {
    const response = await fetch(BACKEND_URL, { method: 'POST', body: formData });
    if (!response.ok) throw new Error('Server error: ' + response.status);

    const responseData = await response.json();

    // Используем встроенную функцию, которая правильно сохраняет всё в sessionStorage
    // передаем: код, режим (не демо), данные с формы, параметры от нейросети
    goToResult(responseData.code, false, payload, responseData.parsed);

  } catch (err) {
    console.error(err);
    showErrors(['Could not reach the server. Check that the backend is running and BACKEND_URL is correct.']);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Generate Python Script';
  }
});
