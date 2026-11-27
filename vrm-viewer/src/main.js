import * as THREE from 'three';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

if (window.__lilyViewerInitialized) {
  console.warn('[lily-viewer] HMR reload detected — skipping re-init');
  if (window.__lilyReconnectWS) window.__lilyReconnectWS();
  throw new Error('lily-viewer.js already initialized');
}
window.__lilyViewerInitialized = true;

// ═══════════════════════════════════════════
// CONSTANTS
// ═══════════════════════════════════════════
const BAR_HEIGHT = 52;
const WIN_W = window.innerWidth;
const WIN_H = window.innerHeight;
const TRANSLATE_MODEL = 'qwen2.5:7b';
const OLLAMA_URL = 'http://127.0.0.1:11434';

// ═══════════════════════════════════════════
// TRANSPARENT BACKGROUND
// ═══════════════════════════════════════════
document.documentElement.style.background = 'transparent';
document.body.style.background = 'transparent';
document.body.style.margin = '0';
document.body.style.overflow = 'hidden';

// ═══════════════════════════════════════════
// SCENE
// ═══════════════════════════════════════════
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(28, WIN_W / WIN_H, 0.1, 20);
camera.position.set(0, 1.35, 2.0);
camera.lookAt(0, 1.35, 0);

let camDragActive = false;
let camLastX = 0, camLastY = 0;

window.addEventListener('mousedown', (e) => {
  if (e.button === 2) { camDragActive = true; camLastX = e.clientX; camLastY = e.clientY; }
});
window.addEventListener('mousemove', (e) => {
  if (camDragActive) {
    const dx = (e.clientX - camLastX) * 0.005;
    const dy = (e.clientY - camLastY) * 0.005;
    camLastX = e.clientX; camLastY = e.clientY;
    camera.position.x -= dx; camera.position.y += dy;
    camera.lookAt(camera.position.x, camera.position.y, 0);
  }
});
window.addEventListener('mouseup', (e) => { if (e.button === 2) camDragActive = false; });

const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, preserveDrawingBuffer: true });
renderer.setClearColor(0x000000, 0); // fully transparent clear
renderer.setSize(WIN_W, WIN_H);
renderer.setPixelRatio(window.devicePixelRatio);
renderer.domElement.style.position = 'fixed';
renderer.domElement.style.left = '0';
renderer.domElement.style.top = '0';
document.body.appendChild(renderer.domElement);

// ── Exit button ──
const exitBtn = document.createElement('button');
exitBtn.textContent = '✕';
Object.assign(exitBtn.style, {
  position: 'fixed', top: '10px', right: '10px', zIndex: '9999',
  width: '32px', height: '32px', borderRadius: '50%',
  border: '1px solid rgba(255,255,255,0.15)', background: 'rgba(30,30,30,0.45)',
  color: '#fff', fontSize: '16px', cursor: 'pointer', lineHeight: '1',
  backdropFilter: 'blur(4px)', transition: 'background 0.2s',
});
exitBtn.addEventListener('mouseenter', () => { exitBtn.style.background = 'rgba(220,50,50,0.8)'; });
exitBtn.addEventListener('mouseleave', () => { exitBtn.style.background = 'rgba(30,30,30,0.45)'; });
exitBtn.addEventListener('click', () => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'shutdown' }));
  setTimeout(() => window.close(), 500);
});
document.body.appendChild(exitBtn);

// ── Code chat button — opens the AI Coding Assistant window ──
const codeChatBtn = document.createElement('button');
codeChatBtn.innerHTML = `<svg style="pointer-events:none" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`;
Object.assign(codeChatBtn.style, {
  position: 'fixed', top: '10px', right: '50px', zIndex: '9999',
  width: '32px', height: '32px', borderRadius: '50%',
  border: '1px solid rgba(255,255,255,0.15)', background: 'rgba(30,30,30,0.45)',
  color: '#fff', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center',
  backdropFilter: 'blur(4px)', transition: 'background 0.2s',
});
codeChatBtn.title = 'Open AI Coding Assistant (Ctrl+Shift+C)';
codeChatBtn.addEventListener('mouseenter', () => { codeChatBtn.style.background = 'rgba(90,110,255,0.8)'; });
codeChatBtn.addEventListener('mouseleave', () => { codeChatBtn.style.background = 'rgba(30,30,30,0.45)'; });
codeChatBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  window.mascotAPI?.openCodeChat();
});
document.body.appendChild(codeChatBtn);

// ── Coding-context badge — lit up when lily_bot.py has detected an
// active coding thread (see extract_coding_context in lily_bot.py) ──
const codeChatBadge = document.createElement('div');
Object.assign(codeChatBadge.style, {
  position: 'fixed', top: '7px', right: '47px', zIndex: '10000',
  width: '9px', height: '9px', borderRadius: '50%',
  background: '#5a6eff', border: '1.5px solid rgba(10,10,18,0.9)',
  display: 'none', pointerEvents: 'none',
  boxShadow: '0 0 6px rgba(90,110,255,0.9)',
  animation: 'micPulse 1.6s ease infinite',
});
document.body.appendChild(codeChatBadge);

let lastCodingContext = null;

function setCodingContextActive(active, summary) {
  codeChatBadge.style.display = active ? 'block' : 'none';
  lastCodingContext = active ? summary : null;
  codeChatBtn.title = active
    ? `Open AI Coding Assistant — active context: ${summary}`
    : 'Open AI Coding Assistant (Ctrl+Shift+C)';
}

// ── Status dot ──
const statusDot = document.createElement('div');
Object.assign(statusDot.style, {
  position: 'fixed', top: '16px', right: '90px', zIndex: '9999',
  width: '8px', height: '8px', borderRadius: '50%',
  background: '#f44', transition: 'background 0.3s',
});
statusDot.title = 'Disconnected';
document.body.appendChild(statusDot);

function updateStatusDot(connected) {
  statusDot.style.background = connected ? '#4f4' : '#f44';
  statusDot.title = connected ? 'Connected' : 'Disconnected';
}

// ── Loading overlay (ATRI/AIRI-style boot sequence) ──
// Created ONCE at module load, not per status-dot update.
const loadingOverlay = document.createElement('div');
Object.assign(loadingOverlay.style, {
  position: 'fixed', inset: '0', zIndex: '20000',
  background: 'rgba(8,8,14,0.94)', backdropFilter: 'blur(6px)',
  display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
  gap: '14px', transition: 'opacity 0.5s ease',
});

const loadingLabel = document.createElement('div');
Object.assign(loadingLabel.style, {
  color: 'rgba(180,230,255,0.9)', fontSize: '13px', letterSpacing: '0.05em',
  fontFamily: '"Consolas", "Courier New", monospace',
});
loadingLabel.textContent = '...';

const barTrack = document.createElement('div');
Object.assign(barTrack.style, {
  width: '260px', height: '6px', borderRadius: '999px',
  background: 'rgba(255,255,255,0.12)', overflow: 'hidden',
});

const barFill = document.createElement('div');
Object.assign(barFill.style, {
  height: '100%', width: '0%', borderRadius: '999px',
  background: 'linear-gradient(90deg, rgba(100,220,255,0.9), rgba(180,255,240,0.9))',
  transition: 'width 0.4s ease',
});
barTrack.appendChild(barFill);

const barPercent = document.createElement('div');
Object.assign(barPercent.style, {
  color: 'rgba(255,255,255,0.5)', fontSize: '11px',
  fontFamily: '"Consolas", "Courier New", monospace',
});
barPercent.textContent = '0%';

loadingOverlay.appendChild(loadingLabel);
loadingOverlay.appendChild(barTrack);
loadingOverlay.appendChild(barPercent);
document.body.appendChild(loadingOverlay);

function updateLoadingProgress(percent, label) {
  const pct = Math.max(0, Math.min(100, percent));
  barFill.style.width = `${pct}%`;
  barPercent.textContent = `${pct}%`;
  if (label) loadingLabel.textContent = label;
  console.log('[loading]', pct, label);
}

function hideLoadingOverlay() {
  loadingOverlay.style.opacity = '0';
  setTimeout(() => loadingOverlay.remove(), 550);
}

// ── Lights ──
const light = new THREE.DirectionalLight(0xffffff, 1.2);
light.position.set(1, 1, 1).normalize();
scene.add(light);
scene.add(new THREE.AmbientLight(0xffffff, 0.6));

const shadowTexture = new THREE.CanvasTexture((() => {
  const c = document.createElement('canvas'); c.width = 128; c.height = 128;
  const ctx = c.getContext('2d');
  const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
  g.addColorStop(0, 'rgba(0,0,0,0.3)'); g.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = g; ctx.fillRect(0, 0, 128, 128);
  return c;
})());
const shadow = new THREE.Mesh(
  new THREE.PlaneGeometry(0.8, 0.8),
  new THREE.MeshBasicMaterial({ map: shadowTexture, transparent: true, depthWrite: false })
);
shadow.rotation.x = -Math.PI / 2; shadow.position.y = 0.01;
scene.add(shadow);

window.addEventListener('wheel', (e) => {
  if (isDragging) return;
  camera.position.z += e.deltaY * 0.005;
  camera.position.z = Math.max(1.5, Math.min(6.0, camera.position.z));
});

// ═══════════════════════════════════════════
// SUBTITLE SYSTEM — LIVE STREAMING (Neuro-sama style)
// ═══════════════════════════════════════════
const subtitleWrap = document.createElement('div');
Object.assign(subtitleWrap.style, {
  position: 'fixed',
  bottom: `${BAR_HEIGHT + 40}px`,
  right: '24px',
  zIndex: '9500',
  pointerEvents: 'none',
  textAlign: 'left',
  maxWidth: '360px',
  transition: 'opacity 0.35s ease',
  opacity: '0',
});

// Small JP original above
const subtitleJp = document.createElement('div');
Object.assign(subtitleJp.style, {
  marginBottom: '4px',
  fontFamily: '"Noto Sans JP", "Yu Gothic", "MS Gothic", sans-serif',
  fontSize: '11px',
  color: 'rgba(255,255,255,0.40)',
  lineHeight: '1.4',
  textShadow: '0 1px 2px rgba(0,0,0,0.8)',
});

// Main English line — with blinking cursor for live feel
const subtitleEn = document.createElement('div');
Object.assign(subtitleEn.style, {
  display: 'inline-block',
  padding: '8px 16px',
  background: 'rgba(0,0,0,0.62)',
  borderRadius: '14px',
  backdropFilter: 'blur(8px)',
  fontFamily: '"Noto Sans", "Segoe UI", Arial, sans-serif',
  fontSize: '14px',
  fontWeight: '500',
  color: '#ffffff',
  lineHeight: '1.55',
  letterSpacing: '0.02em',
  maxWidth: '340px',
  wordBreak: 'break-word',
  textShadow: '0 1px 3px rgba(0,0,0,0.8)',
});

// Blinking cursor element
const cursorEl = document.createElement('span');
Object.assign(cursorEl.style, {
  display: 'none',
  marginLeft: '1px',
  borderRight: '2px solid rgba(255,255,255,0.7)',
  animation: 'subtitleCursorBlink 0.6s step-end infinite',
});
cursorEl.innerHTML = '&nbsp;';

// Cursor blink animation
const cursorStyle = document.createElement('style');
cursorStyle.textContent = `@keyframes subtitleCursorBlink { 0%,100%{opacity:1} 50%{opacity:0} }`;
document.head.appendChild(cursorStyle);

subtitleWrap.appendChild(subtitleJp);
subtitleWrap.appendChild(subtitleEn);
document.body.appendChild(subtitleWrap);

let _subtitleTimer = null;
let _streamAbort = null; // AbortController for current stream

function showSubtitle(en, jp) {
  subtitleEn.textContent = en;
  cursorEl.style.display = 'none';
  subtitleJp.textContent = jp || '';
  subtitleWrap.style.opacity = '1';
  clearTimeout(_subtitleTimer);
  const ms = Math.max(2500, en.length * 55);
  _subtitleTimer = setTimeout(() => { subtitleWrap.style.opacity = '0'; }, ms);
}

function showSubtitleStreaming(jp) {
  // Show JP line and prepare EN area for streaming
  subtitleJp.textContent = jp || '';
  subtitleEn.textContent = '';
  subtitleEn.appendChild(cursorEl);
  cursorEl.style.display = 'inline';
  subtitleWrap.style.opacity = '1';
  clearTimeout(_subtitleTimer);
}

function appendSubtitleToken(token) {
  // Insert token text before the cursor
  const textNode = document.createTextNode(token);
  subtitleEn.insertBefore(textNode, cursorEl);
}

function finishSubtitleStream() {
  cursorEl.style.display = 'none';
  clearTimeout(_subtitleTimer);
  const text = subtitleEn.textContent.trim();
  const ms = Math.max(2500, text.length * 55);
  _subtitleTimer = setTimeout(() => { subtitleWrap.style.opacity = '0'; }, ms);
}

function hideSubtitleSoon(delayMs = 1800) {
  clearTimeout(_subtitleTimer);
  _subtitleTimer = setTimeout(() => { subtitleWrap.style.opacity = '0'; }, delayMs);
}

// ── Streaming translation ─────────────────────────────
const _tQueue = [];
let _tBusy = false;

async function _translateJpStreaming(jpLine) {
  if (!jpLine || !jpLine.trim()) return;

  const cjk = (jpLine.match(/[\u3040-\u30ff\u4e00-\u9fff]/g) || []).length;
  if (cjk < 2) {
    // Not really Japanese — just show as-is
    showSubtitle(jpLine, '');
    return;
  }

  // Cancel any in-progress stream
  if (_streamAbort) { try { _streamAbort.abort(); } catch { } }
  _streamAbort = new AbortController();

  showSubtitleStreaming(jpLine);

  try {
    const res = await fetch(`${OLLAMA_URL}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: _streamAbort.signal,
      body: JSON.stringify({
        model: TRANSLATE_MODEL,
        messages: [{
          role: 'user',
          content:
            `Translate the following Japanese into natural, casual English.\n` +
            `Output ONLY the English translation — no quotes, no labels, nothing else.\n\n` +
            `Japanese: ${jpLine}`,
        }],
        stream: true,  // ← STREAMING MODE
        options: { temperature: 0.1, num_predict: 80 },
      }),
    });

    if (!res.ok) {
      showSubtitle(jpLine, '');
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let fullText = '';
    let skipPrefixDone = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // Ollama streams newline-delimited JSON
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep incomplete line in buffer

      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const obj = JSON.parse(line);
          const token = obj?.message?.content || '';
          if (!token) continue;

          fullText += token;

          // Skip common prefixes like "English: " or opening quotes
          if (!skipPrefixDone) {
            const cleaned = fullText
              .replace(/^["'「」\s]+/, '')
              .replace(/^(English:|Translation:|EN:|Output:)\s*/i, '');
            if (cleaned.length > 0) {
              skipPrefixDone = true;
              // Reset and show the cleaned start
              subtitleEn.textContent = '';
              subtitleEn.appendChild(cursorEl);
              appendSubtitleToken(cleaned);
            }
          } else {
            // Stream each token directly
            const cleanToken = token.replace(/["'「」]$/g, '');
            if (cleanToken) appendSubtitleToken(cleanToken);
          }
        } catch { }
      }
    }

    finishSubtitleStream();
  } catch (e) {
    if (e.name === 'AbortError') return; // cancelled, ok
    console.warn('[Subtitle] stream error:', e);
    showSubtitle(jpLine, '');
  }
}

async function enqueueTranslate(jpLine) {
  _tQueue.push(jpLine);
  if (_tBusy) return;
  _tBusy = true;
  while (_tQueue.length > 0) {
    const next = _tQueue.shift();
    await _translateJpStreaming(next);
  }
  _tBusy = false;
}

// ═══════════════════════════════════════════
// VRM LOADER
// ═══════════════════════════════════════════
const gltfLoader = new GLTFLoader();
gltfLoader.register((parser) => new VRMLoaderPlugin(parser));
let currentVrm = null;
const clock = new THREE.Clock();

gltfLoader.load(
  '/models/lily.vrm',
  (gltf) => {
    const vrm = gltf.userData.vrm;
    VRMUtils.removeUnnecessaryVertices(vrm.scene);
    VRMUtils.combineSkeletons(vrm.scene);
    vrm.scene.traverse((obj) => { obj.frustumCulled = false; });
    vrm.scene.rotation.y = Math.PI;
    scene.add(vrm.scene);
    currentVrm = vrm;
    console.log('VRM loaded:', vrm);
    if (vrm.expressionManager) {
      console.log('Expressions:', vrm.expressionManager.expressions.map(e => e.expressionName));
    }
  },
  (p) => console.log('Loading', (p.loaded / p.total * 100).toFixed(0), '%'),
  (err) => console.error('VRM load error:', err)
);

// ═══════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════
function getBone(vrm, name) { return vrm.humanoid?.getNormalizedBoneNode(name) ?? null; }
function lerp(a, b, f) { return a + (b - a) * f; }
function randRange(min, max) { return min + Math.random() * (max - min); }
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function lerpAngle(a, b, f) {
  let diff = b - a;
  diff = ((diff + Math.PI) % (Math.PI * 2)) - Math.PI;
  if (diff < -Math.PI) diff += Math.PI * 2;
  return a + diff * f;
}
// dt-based exponential smoothing. A fixed per-frame lerp factor (the
// pattern used everywhere below originally) only behaves correctly at
// the frame rate it was tuned at — if the tab throttles, the GPU dips,
// or the window loses focus, every drift/sway/lipsync smoother sped up
// or slowed down together, which reads as inconsistent, "off" motion.
// decay(halfLife, dt) gives the same feel regardless of frame rate.
function decay(halfLife, dt) {
  if (halfLife <= 0) return 1;
  return 1 - Math.pow(0.5, dt / halfLife);
}
// Converts an old "per-frame factor tuned at ~60fps" into an equivalent
// half-life in seconds, so existing tuning doesn't need to be re-guessed.
function speedToHalfLife(speedPerFrame, refFps = 60) {
  const framesToHalf = Math.log(0.5) / Math.log(1 - speedPerFrame);
  return framesToHalf / refFps;
}
const _qa = new THREE.Quaternion();
const _qb = new THREE.Quaternion();

// ═══════════════════════════════════════════
// MOUSE TRACKING + HIT TEST
// ═══════════════════════════════════════════
const mouse = { x: 0, y: 0, active: false, lastMove: 0 };
const lookTarget = { x: 0, y: 0 };

window.addEventListener('mousemove', (e) => { doHitTest(e.clientX, e.clientY); });
window.addEventListener('contextmenu', (e) => e.preventDefault());

const hitPixel = new Uint8Array(4);
let lastHitState = null;

function setHit(isHit) {
  if (isHit !== lastHitState) { lastHitState = isHit; window.mascotAPI?.setHit(isHit); }
}

let isDragging = false;
let lastScreenX = 0, lastScreenY = 0;

window.addEventListener('mousedown', (e) => {
  if (e.button !== 0) return;
  if (lastHitState) { isDragging = true; lastScreenX = e.screenX; lastScreenY = e.screenY; }
});
window.addEventListener('mousemove', (e) => {
  if (isDragging) {
    const dx = e.screenX - lastScreenX, dy = e.screenY - lastScreenY;
    lastScreenX = e.screenX; lastScreenY = e.screenY;
    window.mascotAPI?.dragBy(dx, dy);
  }
});
window.addEventListener('mouseup', (e) => { if (e.button === 0) isDragging = false; });

function doHitTest(clientX, clientY) {
  if (clientX < 0 || clientY < 0) return;
  if (clientY >= WIN_H - BAR_HEIGHT - 16) { setHit(true); return; }
  const el = document.elementFromPoint(clientX, clientY);
  if (el === exitBtn || el === codeChatBtn || el === statusDot || el === chatToggleBtn) { setHit(true); return; }
  if (el && el.closest && el.closest('.float-bubble')) { setHit(true); return; }
  const gl = renderer.getContext();
  const dpr = window.devicePixelRatio;
  const x = Math.floor(clientX * dpr);
  const y = Math.floor((WIN_H - clientY) * dpr);
  if (x < 0 || y < 0 || x >= gl.drawingBufferWidth || y >= gl.drawingBufferHeight) { setHit(false); return; }
  gl.readPixels(x, y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, hitPixel);
  setHit(hitPixel[3] > 10);
}

function updateMouseLook(t) {
  const since = t - mouse.lastMove;
  if (mouse.active && since < 3) {
    lookTarget.x = lerp(lookTarget.x, mouse.x * 0.08, 0.02);
    lookTarget.y = lerp(lookTarget.y, mouse.y * 0.05, 0.02);
  } else {
    lookTarget.x = lerp(lookTarget.x, 0, 0.005);
    lookTarget.y = lerp(lookTarget.y, 0, 0.005);
  }
}

// ═══════════════════════════════════════════
// VMC LIVE MOCAP
// ═══════════════════════════════════════════
const vmcState = { active: false, lastFrameTime: 0, bones: {}, blendshapes: {}, root: null, timeout: 2.0 };
// This half-life now actually drives the live-mocap smoothing below
// (it was declared before but never wired in — bone/hip smoothing was
// silently using a hardcoded, frame-rate-dependent factor instead).
const VMC_SLERP_TIME_CONSTANT = 0.08;

const recorder = { recording: false, frames: [], startTime: 0 };
window.recorder = recorder;

function isTyping() {
  const el = document.activeElement;
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable;
}
window.addEventListener('keydown', (e) => {
  if (isTyping()) return;
  if (e.key === 'r' || e.key === 'R') {
    if (!recorder.recording) {
      recorder.recording = true; recorder.frames = [];
      recorder.startTime = performance.now() / 1000;
      console.log('[REC] Recording started — press R again to stop');
    } else {
      recorder.recording = false;
      console.log('[REC] Stopped. ' + recorder.frames.length + ' frames recorded.');
    }
  }
  const clipNames = { '1': 'idle', '2': 'curious', '3': 'happy', '4': 'sad', '5': 'excited' };
  if (clipNames[e.key] && recorder.frames.length > 0 && !recorder.recording) {
    const blob = new Blob([JSON.stringify(recorder.frames)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = clipNames[e.key] + '_mocap.json'; a.click();
  }
  if (e.key === 'D' && e.shiftKey && recorder.frames.length > 0) {
    const blob = new Blob([JSON.stringify(recorder.frames)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = 'idle_mocap.json'; a.click();
  }
});

function recordFrame() {
  if (!recorder.recording || !vmcState.active) return;
  const t = performance.now() / 1000 - recorder.startTime;
  const bones = {};
  for (const [name, q] of Object.entries(vmcState.bones)) bones[name] = { x: q.qx, y: q.qy, z: q.qz, w: q.qw };
  recorder.frames.push({ t, bones });
}

let _vmcActivationTime = 0;
const VMC_ACTIVATION_BLEND_DURATION = 0.25;

// ── Root translation state — THIS WAS MISSING ENTIRELY BEFORE.
// vmcState.root was populated from every incoming OSC Hips message
// but nothing ever read it — only bone rotation was applied below.
// That's the entire reason walking/entrance never moved her: the
// position data was arriving correctly over the network the whole
// time and just being silently discarded. ──
const _hipsRestPos = new THREE.Vector3();
let _hipsRestCaptured = false;

// Tune these three if she walks in the wrong direction or the motion
// feels too small/large once you see it running — the correct sign
// depends on your VRM's facing (note the vrm.scene.rotation.y = Math.PI
// flip above) and needs eyeballing once, not something guessable from
// code alone.
const ROOT_POS_SCALE = 1.0;
const ROOT_POS_SIGN_X = -1;   // matches the -qx sign flip used for rotation below
const ROOT_POS_SIGN_Z = 1;

function applyVmcFrame(vrm, dt) {
  if (!vmcState.active) return;
  recordFrame();
  const sinceActivation = performance.now() / 1000 - _vmcActivationTime;
  const blendIn = clamp(sinceActivation / VMC_ACTIVATION_BLEND_DURATION, 0, 1);
  // Steady-state smoothing now scales with dt via VMC_SLERP_TIME_CONSTANT
  // instead of a hardcoded 0.6, so it no longer speeds up/slows down
  // with frame rate.
  const rotAlpha = blendIn < 1 ? blendIn : decay(VMC_SLERP_TIME_CONSTANT, dt);

  for (const [boneName, q] of Object.entries(vmcState.bones)) {
    const bone = getBone(vrm, boneName);
    if (!bone) continue;
    const target = new THREE.Quaternion(-q.qx, -q.qy, q.qz, q.qw);
    // A slerp (rather than a hard .copy()) so any per-frame network
    // jitter gets smoothed rather than reproduced exactly. Still tracks
    // the incoming target closely since Python already sends smoothed
    // values at ~60Hz.
    bone.quaternion.slerp(target, rotAlpha);
  }

  // ── Apply root (Hips) position — the actual walking fix. ──
  const hips = getBone(vrm, 'hips');
  if (hips && vmcState.root) {
    if (!_hipsRestCaptured) {
      _hipsRestPos.copy(hips.position);
      _hipsRestCaptured = true;
    }
    const targetPos = new THREE.Vector3(
      _hipsRestPos.x + vmcState.root.px * ROOT_POS_SCALE * ROOT_POS_SIGN_X,
      _hipsRestPos.y + vmcState.root.py * ROOT_POS_SCALE,
      _hipsRestPos.z + vmcState.root.pz * ROOT_POS_SCALE * ROOT_POS_SIGN_Z
    );
    const posAlpha = blendIn < 1 ? blendIn : decay(VMC_SLERP_TIME_CONSTANT, dt);
    hips.position.lerp(targetPos, posAlpha);
  }
}

// ═══════════════════════════════════════════
// MOCAP CLIP PLAYER
// ═══════════════════════════════════════════
const clipPlayer = { clips: {}, current: 'idle', playing: false, startTime: 0, frameIndex: 0 };

async function loadClip(name) {
  try {
    const resp = await fetch(`/animations/${name}_mocap.json`);
    if (!resp.ok) { console.log(`[Clip] ${name}_mocap.json not found (HTTP ${resp.status})`); return; }
    const frames = await resp.json();
    if (frames.length > 0) {
      clipPlayer.clips[name] = frames;
      console.log(`[Clip] Loaded ${name}: ${frames.length} frames`);
    }
  } catch (e) { console.log(`[Clip] ${name}_mocap.json not found`, e); }
}

(async () => {
  await loadClip('idle'); await loadClip('curious'); await loadClip('happy');
  await loadClip('sad'); await loadClip('excited'); await loadClip('angry'); await loadClip('surprised');
  if (clipPlayer.clips['idle']) { clipPlayer.playing = true; clipPlayer.startTime = performance.now() / 1000; }
})();

// Bones actually driven from recorded clip data. Originally this only
// listed torso/head bones — arm rotations were recorded into every
// clip (recordFrame captures every bone in vmcState.bones) but never
// read back during playback, so arms sat frozen at the static standing
// pose the entire time a clip was active. That's the "arms look stiff"
// bug: whenever any *_mocap.json clip loads, this is the code path
// that runs instead of the arm-micro-movement drift below.
const CLIP_BONES = new Set([
  'hips', 'spine', 'chest', 'neck', 'head',
  'leftUpperArm', 'rightUpperArm', 'leftLowerArm', 'rightLowerArm', 'leftHand', 'rightHand',
]);
const CLIP_BLEND_HALF_LIFE = 0.1;

function applyMocapClip(vrm, dt) {
  const emotion = emotionState.current;
  const clipName = clipPlayer.clips[emotion] ? emotion : 'idle';
  if (clipName !== clipPlayer.current && clipPlayer.clips[clipName]) {
    clipPlayer.current = clipName; clipPlayer.frameIndex = 0; clipPlayer.startTime = performance.now() / 1000;
  }
  const frames = clipPlayer.clips[clipPlayer.current];
  if (!frames || frames.length === 0) return false;
  applyStandingPose(vrm, performance.now() / 1000);
  const elapsed = performance.now() / 1000 - clipPlayer.startTime;
  const clipDuration = frames[frames.length - 1].t;
  const loopTime = clipDuration > 0 ? (elapsed % clipDuration) : 0;
  let f0 = frames[0], f1 = frames[0];
  for (let i = 0; i < frames.length - 1; i++) {
    if (frames[i].t <= loopTime && frames[i + 1].t > loopTime) { f0 = frames[i]; f1 = frames[i + 1]; break; }
  }
  const segDur = f1.t - f0.t;
  const alpha = segDur > 0 ? clamp((loopTime - f0.t) / segDur, 0, 1) : 0;
  const FADE_WINDOW = 0.2;
  const fadeStart = clipDuration - FADE_WINDOW;
  let seamBlend = 0;
  if (fadeStart > 0 && loopTime > fadeStart) seamBlend = clamp((loopTime - fadeStart) / FADE_WINDOW, 0, 1);
  const blendA = decay(CLIP_BLEND_HALF_LIFE, dt);
  let appliedCount = 0;
  for (const [boneName, q0] of Object.entries(f0.bones)) {
    if (!CLIP_BONES.has(boneName)) continue;
    const bone = getBone(vrm, boneName); if (!bone) continue; appliedCount++;
    const q1 = f1.bones[boneName] || q0;
    _qa.set(q0.x, q0.y, q0.z, q0.w); _qb.set(q1.x, q1.y, q1.z, q1.w); _qa.slerp(_qb, alpha);
    if (seamBlend > 0) { const sq = frames[0].bones[boneName]; if (sq) { _qb.set(sq.x, sq.y, sq.z, sq.w); _qa.slerp(_qb, seamBlend); } }
    const temp = new THREE.Quaternion(-_qa.x, -_qa.y, _qa.z, _qa.w);
    const cur = bone.quaternion.clone(); cur.slerp(temp, blendA); bone.quaternion.copy(cur);
  }
  return true;
}

// ═══════════════════════════════════════════
// GESTURE OVERLAY
// ═══════════════════════════════════════════
const GESTURES = {
  happy: { head: { x: -0.14, z: 0.09 }, spine: { x: -0.07 }, duration: 0.5 },
  excited: { head: { x: -0.2, y: 0.09 }, spine: { x: -0.14 }, hips: { y: 0.05 }, duration: 0.4, bounce: true },
  sad: { head: { x: 0.16, z: -0.05 }, spine: { x: 0.08 }, duration: 0.7 },
  angry: { head: { x: -0.05, z: -0.1 }, chest: { x: -0.035 }, duration: 0.35 },
  surprised: { head: { x: -0.24 }, spine: { x: -0.1 }, duration: 0.25 },
  curious: { head: { y: 0.24, z: 0.13 }, neck: { y: 0.13 }, duration: 0.6 },
};
const gestureState = { active: null, startTime: 0, duration: 0, lastEmotion: 'neutral' };

function triggerGesture(name) {
  if (!GESTURES[name]) return;
  gestureState.active = GESTURES[name]; gestureState.startTime = performance.now() / 1000;
  gestureState.duration = GESTURES[name].duration;
}
let _lastGestureBounceY = 0;

function applyGestureOverlay(vrm, t) {
  if (!gestureState.active) {
    if (_lastGestureBounceY !== 0) {
      const hips = getBone(vrm, 'hips');
      if (hips) hips.position.y -= _lastGestureBounceY;
      _lastGestureBounceY = 0;
    }
    return;
  }
  const elapsed = t - gestureState.startTime;
  const p = elapsed / gestureState.duration;
  if (p >= 1) {
    gestureState.active = null;
    const hips = getBone(vrm, 'hips');
    if (hips && _lastGestureBounceY !== 0) { hips.position.y -= _lastGestureBounceY; _lastGestureBounceY = 0; }
    return;
  }
  const intensity = p < 0.25 ? (p / 0.25) : (1 - (p - 0.25) / 0.75);
  const g = gestureState.active;
  for (const boneName of Object.keys(g)) {
    if (boneName === 'duration' || boneName === 'bounce') continue;
    const bone = getBone(vrm, boneName); if (!bone) continue;
    const offset = g[boneName];
    if (offset.x) bone.rotation.x += offset.x * intensity;
    if (offset.y) bone.rotation.y += offset.y * intensity;
    if (offset.z) bone.rotation.z += offset.z * intensity;
  }
  if (g.bounce) {
    const hips = getBone(vrm, 'hips');
    if (hips) {
      const bounce = Math.sin(p * Math.PI * 4) * 0.01 * intensity;
      hips.position.y += bounce - _lastGestureBounceY;
      _lastGestureBounceY = bounce;
    }
  }
}



// ═══════════════════════════════════════════
// DRIFT SYSTEM
// ═══════════════════════════════════════════
function createDrift(minVal, maxVal, minHold, maxHold, speed) {
  return {
    current: 0, target: randRange(minVal, maxVal), holdUntil: 0, minVal, maxVal, minHold, maxHold,
    halfLife: speedToHalfLife(speed),
  };
}
function updateDrift(d, t, dt) {
  if (t > d.holdUntil && Math.abs(d.current - d.target) < 0.001) { d.target = randRange(d.minVal, d.maxVal); d.holdUntil = t + randRange(d.minHold, d.maxHold); }
  d.current = lerp(d.current, d.target, decay(d.halfLife, dt));
  return d.current;
}

const weightShift = {
  current: 0,
  target: 0,
  nextChange: 0,
  halfLife: speedToHalfLife(0.008),
};

const drifts = {
  headX: createDrift(-0.045, 0.045, 1.5, 4, 0.015), headY: createDrift(-0.045, 0.045, 2, 6, 0.015),
  headZ: createDrift(-0.022, 0.022, 3, 7, 0.012), neckX: createDrift(-0.022, 0.022, 2, 6, 0.012),
  neckY: createDrift(-0.03, 0.03, 3, 7, 0.012), spineX: createDrift(-0.007, 0.013, 3, 9, 0.007),
  hipsZ: createDrift(-0.011, 0.011, 4, 11, 0.005), hipsY: createDrift(-0.009, 0.009, 5, 11, 0.005),
};

function applyStandingPose(vrm, t) {
  const spine = getBone(vrm, 'spine'), chest = getBone(vrm, 'chest');
  const neck = getBone(vrm, 'neck'), head = getBone(vrm, 'head'), hips = getBone(vrm, 'hips');
  const lua = getBone(vrm, 'leftUpperArm'), rua = getBone(vrm, 'rightUpperArm');
  const lla = getBone(vrm, 'leftLowerArm'), rla = getBone(vrm, 'rightLowerArm');
  const lh = getBone(vrm, 'leftHand'), rh = getBone(vrm, 'rightHand');
  const luleg = getBone(vrm, 'leftUpperLeg'), ruleg = getBone(vrm, 'rightUpperLeg');
  const llleg = getBone(vrm, 'leftLowerLeg'), rlleg = getBone(vrm, 'rightLowerLeg');
  const lfoot = getBone(vrm, 'leftFoot'), rfoot = getBone(vrm, 'rightFoot');
  if (hips) { hips.rotation.x = 0.0; hips.rotation.y = 0.0; hips.rotation.z = -0.03; }
  if (spine) { spine.rotation.x = 0.03; spine.rotation.y = 0.0; spine.rotation.z = 0.015; }
  if (chest) { chest.rotation.x = 0.015; chest.rotation.y = 0.0; chest.rotation.z = 0.01; }
  if (neck) { neck.rotation.x = 0.0; neck.rotation.y = 0.0; neck.rotation.z = -0.01; }
  if (head) { head.rotation.x = 0.0; head.rotation.y = 0.0; head.rotation.z = -0.01; }
  if (lua) lua.rotation.set(0.18, 0.03, 1.32);
  if (rua) rua.rotation.set(0.18, -0.03, -1.32);
  if (lla) lla.rotation.set(-0.10, -0.18, 0.08);
  if (rla) rla.rotation.set(-0.10, 0.18, -0.08);
  if (lh) lh.rotation.set(0.10, 0.0, 0.12);
  if (rh) rh.rotation.set(0.10, 0.0, -0.12);
  if (luleg) luleg.rotation.set(-0.02, 0.0, 0.02);
  if (ruleg) ruleg.rotation.set(0.05, 0.0, -0.03);
  if (llleg) llleg.rotation.set(0.02, 0.0, 0.0);
  if (rlleg) rlleg.rotation.set(-0.04, 0.0, 0.0);
  if (lfoot) lfoot.rotation.set(0.02, 0.0, 0.01);
  if (rfoot) rfoot.rotation.set(-0.01, 0.0, -0.02);
}

function getBreath(t) {
  const base = Math.sin(t * 1.6) * 0.009;
  // Occasional slightly deeper breath. Previously this snapped a full
  // extra 0.003 rad in/out the instant sin(t*0.5) crossed 0.9 — an
  // audible little "pop" once per cycle. Now it ramps smoothly over
  // the last stretch of the approach instead of stepping.
  const slow = Math.sin(t * 0.5);
  const deepEnvelope = clamp((slow - 0.8) / 0.2, 0, 1);
  return base + Math.sin(t * 1.6) * 0.007 * deepEnvelope;
}

// ═══════════════════════════════════════════
// LOCOMOTION
// In VRChat, Neuro doesn't have a body being tracked — Vedal's side
// sends continuous move/turn *axes* (like a virtual joystick), and
// VRChat's own animator blend-trees the walk cycle and feet in
// response. There's no such engine here, so this reproduces the same
// two-layer shape by hand: `locomotion.speed` is the continuous
// "axis" signal, and applyWalkCycle() is our stand-in blend tree that
// swings the leg bones in proportion to it. Gestures/emotions stay a
// separate, discrete layer exactly like VRChat's parameter-triggered
// emotes — that part of your system already matches.
// ═══════════════════════════════════════════
const locomotion = {
  active: false,
  pos: new THREE.Vector3(0, 0, 0),   // offset from her spawn spot
  target: new THREE.Vector3(0, 0, 0),
  speed: 0,                          // 0..1 "how much walk cycle to blend in"
  facing: 0,
  nextWander: performance.now() / 1000 + randRange(8, 16),
};

// Keep her roaming inside a small radius so she doesn't wander off
// the visible desktop area. Tune WALK_SPEED if the walk cycle looks
// too fast/slow for the step size.
const WANDER_RADIUS = 0.0;
const WALK_SPEED = 0.4;
const WALK_FACING_SIGN = -1;
// Keeps her from ever turning far enough to show her back, even
// mid-wander.
const MAX_FACING_YAW = 0.35; // ~20°

function updateLocomotion(vrm, t, dt) {
  if (!locomotion.active && t >= locomotion.nextWander) {
    // Constrain wander targets to a forward-facing arc instead of a
    // full 0..2π circle, so a wander can never send her somewhere
    // that puts her back to the camera.
    const angle = randRange(-Math.PI / 3, Math.PI / 3);
    const r = randRange(0.15, WANDER_RADIUS);
    locomotion.target.set(Math.sin(angle) * r, 0, Math.cos(angle) * r * 0.4 + 0.15);
    locomotion.active = true;
  }

  if (locomotion.active) {
    const toTarget = locomotion.target.clone().sub(locomotion.pos);
    const dist = toTarget.length();
    if (dist < 0.015) {
      locomotion.active = false;
      locomotion.nextWander = t + randRange(8, 18);
    } else {
      toTarget.normalize();
      const step = Math.min(dist, WALK_SPEED * dt);
      locomotion.pos.addScaledVector(toTarget, step);
      const targetYaw = clamp(
        Math.atan2(toTarget.x, toTarget.z) * WALK_FACING_SIGN,
        -MAX_FACING_YAW, MAX_FACING_YAW
      );
      locomotion.facing = lerpAngle(locomotion.facing, targetYaw, decay(0.25, dt));
    }
  } else {
    // FIX: keep easing back toward facing the camera even after
    // she's arrived, instead of freezing at whatever angle the last
    // wander left her at.
    locomotion.facing = lerpAngle(locomotion.facing, 0, decay(0.4, dt));
  }

  const wantSpeed = locomotion.active ? 1 : 0;
  locomotion.speed = lerp(locomotion.speed, wantSpeed, decay(0.3, dt));

  vrm.scene.position.x = locomotion.pos.x;
  vrm.scene.position.z = locomotion.pos.z;
  // FIX: rotation now updates unconditionally (not gated on
  // speed > 0.01), so she keeps correcting back to face-camera even
  // once fully stopped.
  vrm.scene.rotation.y = lerpAngle(vrm.scene.rotation.y, Math.PI + locomotion.facing, decay(0.2, dt));

  applyWalkCycle(vrm, t, locomotion.speed);
}

let _lastWalkBobY = 0;

function applyWalkCycle(vrm, t, speed) {
  const hipsForReset = getBone(vrm, 'hips');
  if (speed < 0.02) {
    if (hipsForReset && _lastWalkBobY !== 0) {
      hipsForReset.position.y -= _lastWalkBobY;
      _lastWalkBobY = 0;
    }
    return;
  }
  const luleg = getBone(vrm, 'leftUpperLeg'), ruleg = getBone(vrm, 'rightUpperLeg');
  const llleg = getBone(vrm, 'leftLowerLeg'), rlleg = getBone(vrm, 'rightLowerLeg');
  const lfoot = getBone(vrm, 'leftFoot'), rfoot = getBone(vrm, 'rightFoot');
  const hips = getBone(vrm, 'hips');
  const lua = getBone(vrm, 'leftUpperArm'), rua = getBone(vrm, 'rightUpperArm');

  const strideFreq = 1.8;
  const phase = t * strideFreq * Math.PI * 2;
  const swing = 0.5 * speed;

  if (luleg) luleg.rotation.x += Math.sin(phase) * swing;
  if (ruleg) ruleg.rotation.x += Math.sin(phase + Math.PI) * swing;
  if (llleg) llleg.rotation.x += Math.max(0, -Math.sin(phase)) * swing * 0.8;
  if (rlleg) rlleg.rotation.x += Math.max(0, -Math.sin(phase + Math.PI)) * swing * 0.8;
  if (lfoot) lfoot.rotation.x += Math.sin(phase + 0.6) * swing * 0.3;
  if (rfoot) rfoot.rotation.x += Math.sin(phase + Math.PI + 0.6) * swing * 0.3;
  // Slight hip bob + opposite arm swing so it doesn't read as legs
  // moving under a static torso.
  if (hips) { const bob = Math.abs(Math.sin(phase)) * 0.01 * speed; hips.position.y += bob - _lastWalkBobY; _lastWalkBobY = bob; }
  if (lua) lua.rotation.x += Math.sin(phase + Math.PI) * swing * 0.3;
  if (rua) rua.rotation.x += Math.sin(phase) * swing * 0.3;
}

function applyDriftOverlay(vrm, t, dt) {
  const breath = getBreath(t);

  const spine = getBone(vrm, 'spine');
  const chest = getBone(vrm, 'chest');
  const hips = getBone(vrm, 'hips');
  const neck = getBone(vrm, 'neck');
  const head = getBone(vrm, 'head');

  const lua = getBone(vrm, 'leftUpperArm');
  const rua = getBone(vrm, 'rightUpperArm');
  const lla = getBone(vrm, 'leftLowerArm');
  const rla = getBone(vrm, 'rightLowerArm');

  // ─────────────────────────────
  // Breathing
  // ─────────────────────────────
  if (spine) spine.rotation.x += breath + updateDrift(drifts.spineX, t, dt);
  if (chest) chest.rotation.x += breath * 0.7;

  if (lua) lua.rotation.x += breath * 0.25;
  if (rua) rua.rotation.x += breath * 0.25;

  if (head) head.position.y += breath * 0.003;

  // ─────────────────────────────
  // Slow body sway / weight shift
  // ─────────────────────────────
  if (t > weightShift.nextChange) {
    weightShift.target = randRange(-0.09, 0.09);
    weightShift.nextChange = t + randRange(4, 9);
  }

  weightShift.current = lerp(
    weightShift.current,
    weightShift.target,
    decay(weightShift.halfLife, dt)
  );

  if (hips) {
    hips.rotation.z += weightShift.current;
    hips.rotation.z += updateDrift(drifts.hipsZ, t, dt);
    hips.rotation.y += updateDrift(drifts.hipsY, t, dt);
  }

  if (spine)
    spine.rotation.z -= weightShift.current * 0.5;

  // ─────────────────────────────
  // Head tracking + idle drift
  // ─────────────────────────────
  if (neck) {
    neck.rotation.x += updateDrift(drifts.neckX, t, dt);
    neck.rotation.y +=
      updateDrift(drifts.neckY, t, dt) +
      lookTarget.x * 0.18;
  }

  if (head) {
    head.rotation.x +=
      updateDrift(drifts.headX, t, dt) -
      lookTarget.y * 0.12;

    head.rotation.y +=
      updateDrift(drifts.headY, t, dt) +
      lookTarget.x * 0.30;

    head.rotation.z +=
      updateDrift(drifts.headZ, t, dt);

    // tiny muscle motion
    head.rotation.x += Math.sin(t * 7.3) * 0.002;
    head.rotation.y += Math.sin(t * 5.9) * 0.0015;
    head.rotation.z += Math.sin(t * 8.1) * 0.001;
  }

  // ─────────────────────────────
  // Arm micro movement
  // ─────────────────────────────
  if (lua)
    lua.rotation.z += Math.sin(t * 0.7) * 0.032;

  if (rua)
    rua.rotation.z += Math.sin(t * 0.7 + 0.8) * 0.032;

  if (lla)
    lla.rotation.x += Math.sin(t * 0.9) * 0.026;

  if (rla)
    rla.rotation.x += Math.sin(t * 0.9 + 0.5) * 0.026;

  // ─────────────────────────────
  // Natural idle head movements
  // ─────────────────────────────
  if (head) {
    // occasional tiny nod
    head.rotation.x += Math.sin(t * 0.18) * 0.018;

    // listening tilt
    head.rotation.z += Math.sin(t * 0.11) * 0.014;

    // tiny curiosity movement
    head.rotation.y += Math.sin(t * 0.15) * 0.012;
  }
}

// ═══════════════════════════════════════════
// BLINK
// ═══════════════════════════════════════════
const blinkState = { next: performance.now() / 1000 + randRange(2, 5), active: false, timer: 0, dur: 0.12, doDouble: false };

function updateBlink(vrm, dt) {
  const expr = vrm.expressionManager; if (!expr) return;
  if (!blinkState.active) {
    blinkState.next -= dt;
    if (blinkState.next <= 0) { blinkState.active = true; blinkState.timer = 0; blinkState.doDouble = Math.random() < 0.12; blinkState.next = 2.5 + Math.random() * 5; }
    expr.setValue('blink', 0);
  } else {
    blinkState.timer += dt; const p = blinkState.timer / blinkState.dur;
    if (p < 0.3) expr.setValue('blink', p / 0.3);
    else if (p < 0.7) expr.setValue('blink', 1.0);
    else if (p < 1.0) expr.setValue('blink', 1 - (p - 0.7) / 0.3);
    else { expr.setValue('blink', 0); if (blinkState.doDouble) { blinkState.doDouble = false; blinkState.timer = 0; } else blinkState.active = false; }
  }
}

// ═══════════════════════════════════════════
// LIPSYNC
// ═══════════════════════════════════════════
const VISEME_CANDIDATES = [['aa', 'ih', 'ou', 'ee', 'oh'], ['A', 'I', 'U', 'E', 'O'], ['Mouth_A', 'Mouth_I', 'Mouth_U', 'Mouth_E', 'Mouth_O'], ['mouthOpen']];
const VISEME_WEIGHTS = [0.40, 0.15, 0.15, 0.20, 0.10];
let activeVisemeKeys = null;

function resolveVisemeKeys(vrm) {
  const expr = vrm.expressionManager; if (!expr) return null;
  const available = new Set(expr.expressions.map(e => e.expressionName));
  for (const c of VISEME_CANDIDATES) { if (c.every(k => available.has(k))) return c; }
  const partial = VISEME_CANDIDATES[0].filter(k => available.has(k));
  if (partial.length > 0) return partial;
  if (available.has('mouthOpen')) return ['mouthOpen'];
  return null;
}

const lipsync = {
  talking: false, targetAmp: 0, smoothAmp: 0, currentViseme: '', nextVisemeChange: 0,
  talkHalfLife: speedToHalfLife(0.3), restHalfLife: speedToHalfLife(0.15),
};

function weightedRandomViseme(keys) {
  if (keys.length === 1) return keys[0];
  const r = Math.random(); let cum = 0;
  for (let i = 0; i < keys.length; i++) { cum += (VISEME_WEIGHTS[i] || (1 / keys.length)); if (r <= cum) return keys[i]; }
  return keys[0];
}

function updateLipsync(vrm, t, dt) {
  const expr = vrm.expressionManager; if (!expr) return;
  if (activeVisemeKeys === null) { activeVisemeKeys = resolveVisemeKeys(vrm) || []; console.log('Viseme keys:', activeVisemeKeys); }
  if (activeVisemeKeys.length === 0) return;
  const halfLife = lipsync.talking ? lipsync.talkHalfLife : lipsync.restHalfLife;
  lipsync.smoothAmp = lerp(lipsync.smoothAmp, lipsync.targetAmp, decay(halfLife, dt));
  if (lipsync.talking && t >= lipsync.nextVisemeChange) { lipsync.currentViseme = weightedRandomViseme(activeVisemeKeys); lipsync.nextVisemeChange = t + randRange(0.07, 0.15); }
  const openAmount = clamp(lipsync.smoothAmp * 0.85, 0, 0.8);
  for (const k of activeVisemeKeys) expr.setValue(k, (k === lipsync.currentViseme) ? openAmount : 0);
  const jaw = getBone(vrm, 'jaw'); if (jaw) jaw.rotation.x = openAmount * 0.25;
}

// ═══════════════════════════════════════════
// EMOTIONS
// ═══════════════════════════════════════════
const EMOTION_BLENDS = {
  neutral: {}, happy: { happy: 0.7 }, excited: { happy: 0.9 }, sad: { sad: 0.7 }, angry: { angry: 0.7 },
  surprised: { surprised: 0.9 }, shy: { happy: 0.3 }, nervous: { sad: 0.2 }, smug: { happy: 0.4 },
  sleepy: { relaxed: 0.6 }, curious: { surprised: 0.2 }, thinking: {}, listening: {}, bored: { relaxed: 0.3 },
};
const EMOTION_KEYS = ['happy', 'sad', 'angry', 'surprised', 'relaxed'];
const emotionState = { current: 'neutral', until: 0 };
// ═══════════════════════════════════════════
// SONG MOOD BOP
// ═══════════════════════════════════════════
const songMoodState = { mood: 'none', energy: 0, active: false };

// Master dial for the whole music-state motion.
// 1.0 = default gentle sway, 0.5 = half as much, 0 = off.
const SONG_BOP_INTENSITY = 1.0;

// Per-mood motion profiles. Rotations are radians, hipLift is metres.
// Every term is a smooth sine. The old version used rectified/abs()
// waves, which have a sharp corner each beat and read as a jerk.
const SONG_BOP_PROFILES = {
  hype: { freq: 1.6,  hipLift: 0.010, headNod: 0.035, spineDip: 0.014, armSwing: 0.025, headTilt: 0.000, spineSway: 0.000 },
  sad:  { freq: 0.35, hipLift: 0.002, headNod: 0.010, spineDip: 0.000, armSwing: 0.000, headTilt: 0.018, spineSway: 0.010 },
  calm: { freq: 1.0,  hipLift: 0.004, headNod: 0.020, spineDip: 0.000, armSwing: 0.000, headTilt: 0.000, spineSway: 0.010 },
};
const SONG_BOP_KEYS = Object.keys(SONG_BOP_PROFILES.calm);

const songBop = {
  env: 0,      // 0..1 fade of the whole effect (no pop when music starts/stops)
  energy: 0,   // smoothed energy
  phase: 0,    // accumulated phase, stays continuous across mood/tempo changes
  params: Object.assign({}, SONG_BOP_PROFILES.calm), // smoothed current profile
  hipY: 0,     // hip offset currently applied, so it can be undone exactly
};

function applySongBop(vrm, dt) {
  const hips = getBone(vrm, 'hips');

  // Live mocap owns the body: don't stack a dance on top of real tracking.
  if (vmcState.active) {
    if (hips && songBop.hipY !== 0) hips.position.y -= songBop.hipY;
    songBop.hipY = 0;
    songBop.env = 0;
    return;
  }

  const wantOn = songMoodState.active;
  songBop.env += ((wantOn ? 1 : 0) - songBop.env) * decay(0.35, dt);
  const targetEnergy = Number.isFinite(songMoodState.energy) ? clamp(songMoodState.energy, 0, 1) : 0;
  songBop.energy += (targetEnergy - songBop.energy) * decay(0.5, dt);

  if (!wantOn && songBop.env < 0.001) {
    if (hips && songBop.hipY !== 0) hips.position.y -= songBop.hipY;
    songBop.hipY = 0;
    songBop.env = 0;
    return;
  }

  // Ease toward the current mood's profile instead of switching instantly.
  if (songMoodState.mood !== 'none') {
    const target = SONG_BOP_PROFILES[songMoodState.mood] || SONG_BOP_PROFILES.calm;
    const a = decay(0.6, dt);
    for (const k of SONG_BOP_KEYS) songBop.params[k] += (target[k] - songBop.params[k]) * a;
  }

  const p = songBop.params;
  songBop.phase = (songBop.phase + dt * p.freq * Math.PI * 2) % (Math.PI * 4);
  const phase = songBop.phase;
  const half = phase * 0.5;
  const amp = songBop.env * songBop.energy * SONG_BOP_INTENSITY;

  const lift = 0.5 - 0.5 * Math.cos(phase);   // smooth 0..1 bounce
  const nod = Math.sin(phase - 0.8);          // head lags the bounce slightly

  const head = getBone(vrm, 'head');
  const spine = getBone(vrm, 'spine');
  const lua = getBone(vrm, 'leftUpperArm');
  const rua = getBone(vrm, 'rightUpperArm');

  if (head) {
    head.rotation.x += p.headNod * nod * amp;
    head.rotation.z += p.headTilt * Math.sin(phase) * amp;
  }
  if (spine) {
    spine.rotation.x -= p.spineDip * lift * amp;
    spine.rotation.z += p.spineSway * Math.sin(half) * amp;
  }
  if (lua) lua.rotation.z += p.armSwing * Math.sin(half) * amp;
  if (rua) rua.rotation.z += p.armSwing * Math.sin(half + Math.PI) * amp;

  const newHip = p.hipLift * lift * amp;
  if (hips) hips.position.y += newHip - songBop.hipY;
  songBop.hipY = newHip;
}

function updateEmotions(vrm, now) {
  const expr = vrm.expressionManager; if (!expr) return;
  if (now > emotionState.until && !lipsync.talking) emotionState.current = 'neutral';
  const targets = EMOTION_BLENDS[emotionState.current] || {};
  for (const name of EMOTION_KEYS) {
    try {
      const cur = expr.getValue(name) || 0;
      const target = targets[name] || 0;
      expr.setValue(name, lerp(cur, target, target > cur ? 0.08 : 0.015));
    } catch { }
  }
}

function applyTalkingHeadBob(vrm, t) {
  if (!lipsync.talking || lipsync.smoothAmp < 0.01) return;
  const head = getBone(vrm, 'head'); if (!head) return;
  head.rotation.x += Math.sin(t * 1.5) * 0.012 * lipsync.smoothAmp;
  head.rotation.z += Math.sin(t * 4.0) * 0.008 * lipsync.smoothAmp;
}

// ═══════════════════════════════════════════
// FLOATING CHAT BUBBLES
// ═══════════════════════════════════════════
const chatStyle = document.createElement('style');
chatStyle.textContent = `
  @keyframes bubbleIn { from { opacity:0; transform:translateY(12px) scale(0.95); } to { opacity:1; transform:none; } }
  @keyframes bubbleOut { from { opacity:1; } to { opacity:0; transform:translateY(-8px); } }
  @keyframes micPulse { 0%,100%{box-shadow:0 0 0 0 rgba(255,60,60,0.4)} 50%{box-shadow:0 0 0 8px rgba(255,60,60,0)} }
  @keyframes blink { 0%,80%,100%{opacity:0.2} 40%{opacity:1} }
  #chat-input:focus { outline: none; }
  .float-bubble {
    position: relative; max-width: 280px; padding: 8px 14px;
    font-size: 13px; line-height: 1.5; word-break: break-word; border-radius: 16px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.4); backdrop-filter: blur(10px);
    text-shadow: 0 1px 2px rgba(0,0,0,0.3); animation: bubbleIn 0.25s ease; pointer-events: auto;
  }
  .float-bubble.lily {
    background: rgba(15,15,25,0.82); color: #fff;
    border: 1px solid rgba(255,255,255,0.12); border-radius: 4px 16px 16px 16px; align-self: flex-start;
  }
  .float-bubble.user {
    background: rgba(110,80,240,0.75); color: #fff;
    border: 1px solid rgba(130,100,255,0.4); border-radius: 16px 4px 16px 16px; align-self: flex-end;
  }
  .float-bubble.fading { animation: bubbleOut 0.6s ease forwards; }
  .float-bubble img.img-preview { max-width: 160px; max-height: 100px; border-radius: 6px; margin-top: 4px; display: block; }
  .float-bubble .code-block { position: relative; margin: 6px 0; }
  .float-bubble .code-block pre {
    background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.12);
    border-radius: 8px; padding: 10px 12px; padding-top: 30px;
    overflow-x: auto; font-family: "SF Mono", Consolas, monospace;
    font-size: 12px; margin: 0; white-space: pre;
  }
  .float-bubble .code-block-actions { position: absolute; top: 5px; right: 5px; display: flex; gap: 5px; z-index: 1; }
  .float-bubble .code-copy-btn {
    background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.18);
    color: rgba(255,255,255,0.75); font-size: 10px; padding: 2px 7px;
    border-radius: 5px; cursor: pointer;
  }
  .float-bubble .code-copy-btn:hover { color: #fff; }
  .float-bubble .code-save-btn { border-color: rgba(120,90,255,0.7); color: rgba(170,150,255,1); }
  .float-bubble .code-edit-textarea {
    width: 100%; background: rgba(0,0,0,0.45); border: 1px solid rgba(120,90,255,0.6);
    border-radius: 8px; color: #fff; font-family: "SF Mono", Consolas, monospace;
    font-size: 12px; padding: 10px 12px; resize: vertical; box-sizing: border-box;
  }
  .typing-dots {
    padding: 8px 14px; border-radius: 4px 16px 16px 16px;
    background: rgba(15,15,25,0.82); border: 1px solid rgba(255,255,255,0.12);
    backdrop-filter: blur(10px); display: flex; gap: 4px; align-items: center; align-self: flex-start;
  }
  .typing-dots span { width: 5px; height: 5px; border-radius: 50%; background: rgba(255,255,255,0.5); animation: blink 1.2s infinite; }
  .typing-dots span:nth-child(2) { animation-delay: 0.4s; }
  .typing-dots span:nth-child(3) { animation-delay: 0.8s; }
`;
document.head.appendChild(chatStyle);

const bubbleContainer = document.createElement('div');
Object.assign(bubbleContainer.style, {
  position: 'fixed', bottom: `${BAR_HEIGHT + 20}px`, right: '16px',
  width: '320px', maxHeight: '45vh', display: 'none', flexDirection: 'column', gap: '8px',
  pointerEvents: 'none', zIndex: '8888', overflowY: 'hidden',
});
document.body.appendChild(bubbleContainer);

const typingEl = document.createElement('div');
typingEl.className = 'typing-dots';
typingEl.style.display = 'none';
typingEl.innerHTML = '<span></span><span></span><span></span>';

const BUBBLE_LIFETIME = 12000;
const MAX_VISIBLE_BUBBLES = 6;

// ── Renders text into a bubble, splitting out ``` fenced code blocks
// so Lily's replies get syntax-block styling + Copy/Edit, same as the
// coding assistant window. User bubbles and plain text still just use
// textContent (no code parsing needed on the user's own messages). ──
function renderBubbleContent(bubbleEl, text) {
  const parts = text.split(/```(\w*)\n?/);
  let i = 0;
  while (i < parts.length) {
    const plain = parts[i];
    if (plain) {
      const span = document.createElement('span');
      span.textContent = plain;
      bubbleEl.appendChild(span);
    }
    const code = parts[i + 2];
    if (code !== undefined) {
      bubbleEl.appendChild(buildCodeBlock(code.replace(/\n$/, '')));
    }
    i += 3;
  }
}
function buildCodeBlock(codeText) {
  const wrap = document.createElement('div');
  wrap.className = 'code-block';

  const pre = document.createElement('pre');
  const codeEl = document.createElement('code');
  codeEl.textContent = codeText;
  pre.appendChild(codeEl);

  const actions = document.createElement('div');
  actions.className = 'code-block-actions';

  const copyBtn = document.createElement('button');
  copyBtn.className = 'code-copy-btn';
  copyBtn.textContent = 'Copy';
  copyBtn.onclick = async (e) => {
    e.stopPropagation();
    const text = codeEl.textContent;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        throw new Error('clipboard API unavailable');
      }
      copyBtn.textContent = 'Copied!';
    } catch (err) {
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        copyBtn.textContent = 'Copied!';
      } catch (fallbackErr) {
        console.error('[copy] both clipboard methods failed:', err, fallbackErr);
        copyBtn.textContent = 'Failed';
      }
    }
    setTimeout(() => (copyBtn.textContent = 'Copy'), 1200);
  };

  const editBtn = document.createElement('button');
  editBtn.className = 'code-copy-btn';
  editBtn.textContent = 'Edit';
  editBtn.onclick = (e) => {
    e.stopPropagation();
    enterCodeEditMode(wrap, pre, codeEl, actions);
  };

  actions.appendChild(copyBtn);
  actions.appendChild(editBtn);
  wrap.appendChild(actions);
  wrap.appendChild(pre);
  return wrap;
}

function enterCodeEditMode(wrap, pre, codeEl, actions) {
  const textarea = document.createElement('textarea');
  textarea.className = 'code-edit-textarea';
  textarea.value = codeEl.textContent;
  textarea.rows = Math.min(20, codeEl.textContent.split('\n').length + 1);
  textarea.spellcheck = false;
  textarea.addEventListener('mousedown', (e) => e.stopPropagation());

  const editActions = document.createElement('div');
  editActions.className = 'code-block-actions';

  const saveBtn = document.createElement('button');
  saveBtn.className = 'code-copy-btn code-save-btn';
  saveBtn.textContent = 'Save';
  saveBtn.onclick = (e) => {
    e.stopPropagation();
    codeEl.textContent = textarea.value;
    wrap.replaceChild(pre, textarea);
    wrap.replaceChild(actions, editActions);
  };

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'code-copy-btn';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.onclick = (e) => {
    e.stopPropagation();
    wrap.replaceChild(pre, textarea);
    wrap.replaceChild(actions, editActions);
  };

  editActions.appendChild(saveBtn);
  editActions.appendChild(cancelBtn);
  wrap.replaceChild(textarea, pre);
  wrap.replaceChild(editActions, actions);
  textarea.focus();
}

function addMessage(role, text, imageDataUrl) {
  const isLily = role === 'lily';
  const bubble = document.createElement('div');
  bubble.className = `float-bubble ${isLily ? 'lily' : 'user'}`;

  if (isLily && text.includes('```')) {
    renderBubbleContent(bubble, text);
  } else {
    bubble.textContent = text;
  }

  if (imageDataUrl) {
    const img = document.createElement('img'); img.src = imageDataUrl; img.className = 'img-preview'; bubble.appendChild(img);
  }
  if (bubbleContainer.contains(typingEl)) bubbleContainer.removeChild(typingEl);
  bubbleContainer.appendChild(bubble);
  bubbleContainer.appendChild(typingEl);
  const bubbles = bubbleContainer.querySelectorAll('.float-bubble');
  if (bubbles.length > MAX_VISIBLE_BUBBLES) { for (let i = 0; i < bubbles.length - MAX_VISIBLE_BUBBLES; i++) bubbles[i].remove(); }
  setTimeout(() => {
    if (bubble.parentNode) { bubble.classList.add('fading'); setTimeout(() => bubble.remove(), 600); }
  }, BUBBLE_LIFETIME);
}


// ═══════════════════════════════════════════
// BOTTOM BAR (retractable)
// ═══════════════════════════════════════════
const bottomBar = document.createElement('div');
Object.assign(bottomBar.style, {
  position: 'fixed', bottom: '0', left: '0', width: `${WIN_W}px`,
  height: `${BAR_HEIGHT}px`, maxHeight: `${BAR_HEIGHT}px`, minHeight: `${BAR_HEIGHT}px`,
  overflow: 'hidden', boxSizing: 'border-box',
  background: 'rgba(10,10,18,0.45)', backdropFilter: 'blur(12px)',
  borderTop: '1px solid rgba(255,255,255,0.08)',
  display: 'flex', alignItems: 'center', padding: '0 12px', gap: '6px', zIndex: '9000',
  transform: 'translateY(100%)', transition: 'transform 0.25s ease', pointerEvents: 'auto',
});
document.body.appendChild(bottomBar);
bottomBar.addEventListener('mousedown', (e) => e.stopPropagation());

let chatBarOpen = false;

// ── Small always-visible toggle pill ──
const chatToggleBtn = document.createElement('button');
chatToggleBtn.innerHTML = `<svg style="pointer-events:none" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;
Object.assign(chatToggleBtn.style, {
  position: 'fixed', bottom: '14px', left: '14px', zIndex: '9100',
  width: '34px', height: '34px', borderRadius: '50%', border: 'none',
  background: 'rgba(30,30,40,0.55)', color: 'rgba(255,255,255,0.75)',
  cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center',
  backdropFilter: 'blur(8px)', transition: 'background 0.2s, transform 0.2s',
  boxShadow: '0 2px 8px rgba(0,0,0,0.35)',
});
chatToggleBtn.title = 'Show/hide text input';
chatToggleBtn.addEventListener('mouseenter', () => { chatToggleBtn.style.background = 'rgba(120,90,255,0.6)'; });
chatToggleBtn.addEventListener('mouseleave', () => { chatToggleBtn.style.background = chatBarOpen ? 'rgba(120,90,255,0.6)' : 'rgba(30,30,40,0.55)'; });
document.body.appendChild(chatToggleBtn);

function setChatBarOpen(open) {
  chatBarOpen = open;
  bottomBar.style.transform = open ? 'translateY(0)' : 'translateY(100%)';
  chatToggleBtn.style.background = open ? 'rgba(120,90,255,0.6)' : 'rgba(30,30,40,0.55)';
  chatToggleBtn.style.bottom = open ? `${BAR_HEIGHT + 14}px` : '14px';
  if (open) setTimeout(() => chatInput.focus(), 260);
}
chatToggleBtn.addEventListener('click', (e) => { e.stopPropagation(); setChatBarOpen(!chatBarOpen); });

const uploadBtn = document.createElement('button');
uploadBtn.innerHTML = `<svg style="pointer-events:none" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/></svg>`;
Object.assign(uploadBtn.style, {
  width: '34px', height: '34px', borderRadius: '50%', border: 'none',
  background: 'rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.6)',
  cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: '0', transition: 'background 0.2s',
});
uploadBtn.title = 'Attach image';
uploadBtn.addEventListener('mouseenter', () => { if (!pendingImageB64) uploadBtn.style.background = 'rgba(255,255,255,0.18)'; });
uploadBtn.addEventListener('mouseleave', () => { if (!pendingImageB64) uploadBtn.style.background = 'rgba(255,255,255,0.08)'; });

const fileInput = document.createElement('input');
fileInput.type = 'file'; fileInput.accept = 'image/*'; fileInput.style.display = 'none';
document.body.appendChild(fileInput);

let pendingImageData = null;
let pendingImageB64 = null;

uploadBtn.addEventListener('click', (e) => { e.stopPropagation(); fileInput.click(); });
fileInput.addEventListener('change', () => {
  const file = fileInput.files?.[0];
  console.log('[fileInput] change fired', file?.name, file?.type, file?.size);
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    pendingImageData = reader.result;
    pendingImageB64 = reader.result.split(',')[1];
    console.log('[fileInput] read complete, b64 length:', pendingImageB64?.length);
    uploadBtn.style.background = 'rgba(110,80,240,0.5)'; uploadBtn.style.color = '#fff';
    uploadBtn.title = `${file.name} — send to include`;
  };
  reader.onerror = (err) => console.error('[fileInput] FileReader error', err);
  reader.readAsDataURL(file);
  fileInput.value = '';
});

const chatInput = document.createElement('input');
chatInput.id = 'chat-input';
chatInput.placeholder = 'Message Lily...';
Object.assign(chatInput.style, {
  flex: '1', minWidth: '0', height: '34px', maxHeight: '34px', boxSizing: 'border-box',
  background: 'rgba(255,255,255,0.07)', border: '1px solid rgba(255,255,255,0.12)',
  borderRadius: '20px', padding: '8px 16px', color: '#fff', fontSize: '13px',
  caretColor: 'rgba(255,255,255,0.7)',
});
chatInput.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { setChatBarOpen(false); }
});
function looksLikeCode(text) {
  const lines = text.split('\n');
  if (lines.length < 3) return false;
  const codeSignals = /[{};]|=>|^\s*(function|const|let|var|import|export|def|class|if|for|while)\b|<\/?\w+>|^\s{2,}\S/m;
  return codeSignals.test(text);
}

function wrapAsCodeBlock(text) {
  if (text.trim().startsWith('```')) return text;
  return '```\n' + text.replace(/\n$/, '') + '\n```';
}

chatInput.addEventListener('paste', (e) => {
  const text = e.clipboardData?.getData('text');
  if (!text || !looksLikeCode(text)) return;
  e.preventDefault();
  const start = chatInput.selectionStart, end = chatInput.selectionEnd;
  const wrapped = wrapAsCodeBlock(text);
  chatInput.value = chatInput.value.slice(0, start) + wrapped + chatInput.value.slice(end);
  const pos = start + wrapped.length;
  chatInput.selectionStart = chatInput.selectionEnd = pos;
});

const micBtn = document.createElement('button');
micBtn.innerHTML = `<svg style="pointer-events:none" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>`;
Object.assign(micBtn.style, {
  width: '34px', height: '34px', borderRadius: '50%', border: 'none',
  background: 'rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.6)',
  cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: '0', transition: 'all 0.2s',
});
micBtn.title = 'Toggle voice input';
micBtn.addEventListener('mouseenter', () => { if (!micListening) micBtn.style.background = 'rgba(255,255,255,0.18)'; });
micBtn.addEventListener('mouseleave', () => { if (!micListening) micBtn.style.background = 'rgba(255,255,255,0.08)'; });

let micListening = false;

function setMicActive(active) {
  micListening = active;
  if (active) {
    micBtn.style.background = 'rgba(255,60,60,0.6)'; micBtn.style.color = '#fff';
    micBtn.style.animation = 'micPulse 1.5s ease infinite'; micBtn.title = 'Voice mode ON — click to stop';
  } else {
    micBtn.style.background = 'rgba(255,255,255,0.08)'; micBtn.style.color = 'rgba(255,255,255,0.6)';
    micBtn.style.animation = 'none'; micBtn.title = 'Toggle voice input';
  }
}

micBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const newMode = micListening ? 'text mode' : 'voice mode';
  ws.send(JSON.stringify({ type: 'chat_input', text: newMode }));
  setMicActive(!micListening);
});

const sendBtn = document.createElement('button');
sendBtn.innerHTML = '↑';
Object.assign(sendBtn.style, {
  width: '34px', height: '34px', borderRadius: '50%', border: 'none',
  background: 'rgba(120,90,255,0.65)', color: '#fff', fontSize: '17px',
  cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center',
  flexShrink: '0', transition: 'background 0.2s, transform 0.1s',
});
sendBtn.addEventListener('mouseenter', () => { sendBtn.style.background = 'rgba(120,90,255,1)'; sendBtn.style.transform = 'scale(1.05)'; });
sendBtn.addEventListener('mouseleave', () => { sendBtn.style.background = 'rgba(120,90,255,0.65)'; sendBtn.style.transform = 'none'; });

let lastSendTime = 0, lastSendText = '', sendInFlight = false;

function sendChat() {
  const text = chatInput.value.trim();
  console.log('[sendChat] fired', { text, hasImage: !!pendingImageB64, wsState: ws?.readyState });
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn('[sendChat] aborted — WS not open. readyState:', ws?.readyState);
    return;
  }
  if (!text && !pendingImageB64) {
    console.warn('[sendChat] aborted — no text and no image');
    return;
  }
  const now = performance.now();
  if (sendInFlight) { console.warn('[sendChat] aborted — send already in flight'); return; }
  if (text === lastSendText && (now - lastSendTime) < 3000) {
    console.warn('[sendChat] aborted — duplicate text within 3s cooldown');
    return;
  }
  sendInFlight = true; lastSendTime = now; lastSendText = text;
  const payload = { type: 'chat_input', text: text || '(image)' };
  if (pendingImageB64) payload.image = pendingImageB64;
  console.log('[sendChat] sending payload', { type: payload.type, textLen: payload.text.length, hasImage: !!payload.image, imageLen: payload.image?.length });
  ws.send(JSON.stringify(payload));
  addMessage('user', text || '', pendingImageData || null);
  chatInput.value = '';
  if (pendingImageData) {
    pendingImageData = null; pendingImageB64 = null;
    uploadBtn.style.background = 'rgba(255,255,255,0.08)'; uploadBtn.style.color = 'rgba(255,255,255,0.6)'; uploadBtn.title = 'Attach image';
  }
  typingEl.style.display = 'flex';
  setTimeout(() => { sendInFlight = false; }, 500);
}


sendBtn.addEventListener('click', sendChat);
chatInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(); }
});

bottomBar.appendChild(uploadBtn);
bottomBar.appendChild(chatInput);
bottomBar.appendChild(micBtn);
bottomBar.appendChild(sendBtn);
// ═══════════════════════════════════════════
// WEBSOCKET  — subtitle translation built in
// ═══════════════════════════════════════════
const lilyState = { connected: false };
let ws = null;
let wsConnecting = false;

function connectWS() {
  if (wsConnecting) return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  wsConnecting = true;
  try {
    ws = new WebSocket(`ws://${window.location.hostname}:8765`);
    ws.onopen = () => {
      wsConnecting = false; lilyState.connected = true; updateStatusDot(true);
      console.log('[WS] Connected');
    };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);

        if (msg.type === 'vmc_frame') {
          if (!vmcState.active) _vmcActivationTime = performance.now() / 1000;
          vmcState.bones = msg.bones || {}; vmcState.blendshapes = msg.blendshapes || {};
          vmcState.root = msg.root || null; vmcState.lastFrameTime = performance.now() / 1000; vmcState.active = true;
        }
        else if (msg.type === 'talk_start') { lipsync.talking = true; lipsync.nextVisemeChange = 0; }
        else if (msg.type === 'talk_stop') {
          lipsync.talking = false; lipsync.targetAmp = 0;
          hideSubtitleSoon(2000);
        }
        else if (msg.type === 'talk_amp') { lipsync.targetAmp = Number(msg.value ?? 0); }
        else if (msg.type === 'emotion') {
          const name = msg.name ?? 'neutral';
          emotionState.current = name;
          emotionState.until = performance.now() / 1000 + (msg.duration ?? 12);
          if (!vmcState.active && name !== gestureState.lastEmotion) {
            triggerGesture(name); gestureState.lastEmotion = name;
          }
        }
        else if (msg.type === 'chat_message') {
          typingEl.style.display = 'none';
          const speaker = msg.speaker ?? 'lily';
          const text = msg.text ?? '';
          if (speaker === 'lily' && text) {
            enqueueTranslate(text);   // streams live
          }
        }
        else if (msg.type === 'shutdown_ack') { window.close(); }
        else if (msg.type === 'loading_progress') {
          updateLoadingProgress(msg.percent ?? 0, msg.label);
        }
        else if (msg.type === 'loading_complete') {
          updateLoadingProgress(100, 'Ready');
          hideLoadingOverlay();
        }
        else if (msg.type === 'song_mood') {
          songMoodState.mood = msg.mood ?? 'none';
          songMoodState.energy = Number.isFinite(Number(msg.energy)) ? Number(msg.energy) : 0;
          songMoodState.active = songMoodState.mood !== 'none';
        }
        else if (msg.type === 'coding_context') {
          setCodingContextActive(!!msg.active, msg.summary || '');
        }

      } catch (err) { console.error('[WS parse]', err); }
    };
    ws.onclose = (event) => {
      console.log('[WS] Close code:', event.code, 'reason:', event.reason, 'clean:', event.wasClean);
      wsConnecting = false; lilyState.connected = false; updateStatusDot(false);
      lipsync.talking = false; lipsync.targetAmp = 0; vmcState.active = false;
      console.log('[WS] Disconnected, retry 3s'); setTimeout(connectWS, 3000);
    };
    ws.onerror = () => { try { ws.close(); } catch { } };
  } catch { wsConnecting = false; setTimeout(connectWS, 3000); }
}
connectWS();
window.__lilyReconnectWS = connectWS;

// ═══════════════════════════════════════════
// RENDER LOOP
// ═══════════════════════════════════════════
let _vmcHasBlendshapes = false;
let _vmcBlendshapeCheckTime = 0;

function animate() {
  requestAnimationFrame(animate);
  const dt = Math.min(clock.getDelta(), 1 / 30), t = clock.getElapsedTime(), now = performance.now() / 1000;
  if (!currentVrm) { renderer.render(scene, camera); return; }

  updateMouseLook(t);

  if (vmcState.active && (now - vmcState.lastFrameTime) > vmcState.timeout) {
    vmcState.active = false; _vmcHasBlendshapes = false;
  }
  if (vmcState.active && now - _vmcBlendshapeCheckTime > 1.0) {
    _vmcHasBlendshapes = Object.keys(vmcState.blendshapes).length > 0; _vmcBlendshapeCheckTime = now;
  }

  if (vmcState.active) {
    applyVmcFrame(currentVrm, dt);
    if (!_vmcHasBlendshapes) { updateBlink(currentVrm, dt); updateEmotions(currentVrm, now); }
    updateLipsync(currentVrm, t, dt);
  } else {
    const gestureActive = !!gestureState.active;
    const clipActive = !gestureActive && applyMocapClip(currentVrm, dt);
    if (!clipActive) {
      applyStandingPose(currentVrm, t);
      applyDriftOverlay(currentVrm, t, dt);
      // updateLocomotion(currentVrm, t, dt); // disabled -- keep her standing still
    }
    applyGestureOverlay(currentVrm, t);
    updateBlink(currentVrm, dt);
    updateLipsync(currentVrm, t, dt);
    updateEmotions(currentVrm, now);
  }

  // Song bop and talking head-bob apply as overlays regardless of which
  // branch above ran, so they only need to be called once per frame here
  // (previously called once at the top AND again inside the else branch).
  applySongBop(currentVrm, dt);
  applyTalkingHeadBob(currentVrm, t);

  currentVrm.update(dt);
  renderer.render(scene, camera);
}
animate();

window._debug = { vmcState, clipPlayer, lipsync, emotionState, get vmcHasBlendshapes() { return _vmcHasBlendshapes; } };