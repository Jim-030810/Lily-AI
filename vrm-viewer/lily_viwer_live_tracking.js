import * as THREE from 'three';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(30, window.innerWidth / window.innerHeight, 0.1, 20);
camera.position.set(0, 0.75, 3.5);
camera.lookAt(0, 0.75, 0);

const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
renderer.setSize(window.innerWidth, window.innerHeight);
document.body.appendChild(renderer.domElement);

const light = new THREE.DirectionalLight(0xffffff, 1.2);
light.position.set(1, 1, 1).normalize();
scene.add(light);
scene.add(new THREE.AmbientLight(0xffffff, 0.6));

// Ground shadow
const shadowTexture = new THREE.CanvasTexture((() => {
    const c = document.createElement('canvas');
    c.width = 128; c.height = 128;
    const ctx = c.getContext('2d');
    const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
    g.addColorStop(0, 'rgba(0,0,0,0.3)');
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, 128, 128);
    return c;
})());
const shadow = new THREE.Mesh(
    new THREE.PlaneGeometry(0.8, 0.8),
    new THREE.MeshBasicMaterial({ map: shadowTexture, transparent: true, depthWrite: false })
);
shadow.rotation.x = -Math.PI / 2;
shadow.position.y = 0.01;
scene.add(shadow);

const gltfLoader = new GLTFLoader();
gltfLoader.register((parser) => new VRMLoaderPlugin(parser));
let currentVrm = null;

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
        loadMocap();
    },
    (progress) => console.log('Loading', (progress.loaded / progress.total) * 100, '%'),
    (error) => console.error('VRM load error:', error)
);

// ═══════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════
function getBone(vrm, name) {
    return vrm.humanoid?.getNormalizedBoneNode(name) ?? null;
}
function lerp(a, b, f) { return a + (b - a) * f; }
function randRange(min, max) { return min + Math.random() * (max - min); }

// ═══════════════════════════════════════════
// LIVE WEBCAM MOTION TRACKING (MediaPipe Pose)
// Tracks your real body movement in real time and
// retargets it onto Lily's bones every frame.
// Falls back to recorded mocap loop, then drift,
// if the webcam/tracking isn't available.
// ═══════════════════════════════════════════
let liveLandmarks = null;
let liveTrackingActive = false;
let pose = null;

function setupLiveTracking() {
    // Small preview window for the tracking webcam feed
    const previewWrap = document.createElement('div');
    previewWrap.style.position = 'fixed';
    previewWrap.style.bottom = '16px';
    previewWrap.style.right = '16px';
    previewWrap.style.width = '160px';
    previewWrap.style.height = '120px';
    previewWrap.style.borderRadius = '8px';
    previewWrap.style.overflow = 'hidden';
    previewWrap.style.border = '2px solid rgba(255,255,255,0.25)';
    previewWrap.style.boxShadow = '0 4px 12px rgba(0,0,0,0.4)';
    previewWrap.style.zIndex = '1000';
    previewWrap.style.background = '#000';

    const previewVideo = document.createElement('video');
    previewVideo.autoplay = true;
    previewVideo.playsInline = true;
    previewVideo.muted = true;
    previewVideo.style.width = '100%';
    previewVideo.style.height = '100%';
    previewVideo.style.objectFit = 'cover';
    previewVideo.style.transform = 'scaleX(-1)'; // mirror, feels natural

    const label = document.createElement('div');
    label.textContent = 'Live tracking';
    label.style.position = 'absolute';
    label.style.bottom = '4px';
    label.style.left = '6px';
    label.style.fontSize = '11px';
    label.style.color = '#fff';
    label.style.fontFamily = 'system-ui';
    label.style.textShadow = '0 1px 2px rgba(0,0,0,0.8)';
    previewWrap.style.position = 'fixed'; // ensure positioning context for label

    previewWrap.appendChild(previewVideo);
    previewWrap.appendChild(label);
    document.body.appendChild(previewWrap);

    pose = new Pose({
        locateFile: (file) => `https://cdn.jsdelivr.net/npm/@mediapipe/pose@0.5.1675469404/${file}`
    });
    pose.setOptions({
        modelComplexity: 1,
        smoothLandmarks: true,
        minDetectionConfidence: 0.5,
        minTrackingConfidence: 0.5,
    });
    pose.onResults((results) => {
        liveLandmarks = results.poseLandmarks || null;
    });

    navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480, facingMode: 'user' } })
        .then((stream) => {
            previewVideo.srcObject = stream;
            return new Promise((resolve) => { previewVideo.onloadedmetadata = resolve; });
        })
        .then(() => {
            liveTrackingActive = true;
            console.log('[LiveTracking] Webcam ready, tracking started');
            trackLoop(previewVideo);
        })
        .catch((e) => {
            console.warn('[LiveTracking] Webcam unavailable, falling back to mocap/drift:', e.message);
            liveTrackingActive = false;
            previewWrap.remove();
        });
}

async function trackLoop(videoEl) {
    if (!liveTrackingActive) return;
    try {
        await pose.send({ image: videoEl });
    } catch (e) {
        // transient errors are fine, keep looping
    }
    requestAnimationFrame(() => trackLoop(videoEl));
}

// Same retargeting math as the recorder — converts MediaPipe
// landmarks into normalized bone rotations for the VRM.
function landmarksToBoneRotations(landmarks) {
    if (!landmarks || landmarks.length < 25) return null;

    const lm = landmarks;

    function dir(a, b) {
        return { x: lm[b].x - lm[a].x, y: lm[b].y - lm[a].y, z: (lm[b].z || 0) - (lm[a].z || 0) };
    }

    const midShoulderX = (lm[11].x + lm[12].x) / 2;
    const midShoulderY = (lm[11].y + lm[12].y) / 2;
    const midHipX = (lm[23].x + lm[24].x) / 2;
    const midHipY = (lm[23].y + lm[24].y) / 2;
    const spineDir = { x: midShoulderX - midHipX, y: midShoulderY - midHipY };
    const spineLean = Math.atan2(spineDir.x, -spineDir.y);

    const noseRelX = lm[0].x - midShoulderX;
    const noseRelY = lm[0].y - midShoulderY;
    const headYaw = noseRelX * 2.5;
    const headPitch = (noseRelY + 0.15) * 1.5;

    const shoulderDir = dir(12, 11);
    const shoulderTilt = Math.atan2(shoulderDir.y, shoulderDir.x);

    const leftUpperArmDir = dir(11, 13);
    const rightUpperArmDir = dir(12, 14);
    const leftLowerArmDir = dir(13, 15);
    const rightLowerArmDir = dir(14, 16);

    const leftArmAngleZ = Math.atan2(leftUpperArmDir.x, leftUpperArmDir.y);
    const rightArmAngleZ = Math.atan2(rightUpperArmDir.x, rightUpperArmDir.y);
    const leftElbowAngle = Math.atan2(leftLowerArmDir.y, leftLowerArmDir.x) -
        Math.atan2(leftUpperArmDir.y, leftUpperArmDir.x);
    const rightElbowAngle = Math.atan2(rightLowerArmDir.y, rightLowerArmDir.x) -
        Math.atan2(rightUpperArmDir.y, rightUpperArmDir.x);

    const hipDir = dir(24, 23);
    const hipTilt = Math.atan2(hipDir.y, hipDir.x);

    return {
        head: { x: headPitch * 0.4, y: -headYaw * 0.5, z: shoulderTilt * 0.3 },
        neck: { x: headPitch * 0.15, y: -headYaw * 0.2, z: 0 },
        spine: { x: spineLean * 0.15, y: 0, z: spineLean * 0.3 },
        hips: { x: 0, y: hipTilt * 0.1, z: hipTilt * 0.2 },
        leftUpperArm: { x: 0, y: 0, z: leftArmAngleZ * 0.8 },
        rightUpperArm: { x: 0, y: 0, z: rightArmAngleZ * 0.8 },
        leftLowerArm: { x: 0, y: leftElbowAngle * 0.3, z: 0 },
        rightLowerArm: { x: 0, y: rightElbowAngle * 0.3, z: 0 },
        leftShoulder: { x: 0, y: 0, z: shoulderTilt * 0.1 },
        rightShoulder: { x: 0, y: 0, z: -shoulderTilt * 0.1 },
    };
}

// Smooths live tracking frame-to-frame to avoid MediaPipe jitter.
let smoothedLiveBones = null;
function smoothLiveBones(target, smoothing = 0.35) {
    if (!target) return smoothedLiveBones;
    if (!smoothedLiveBones) { smoothedLiveBones = target; return target; }
    const out = {};
    for (const bone of Object.keys(target)) {
        out[bone] = {
            x: lerp(smoothedLiveBones[bone]?.x ?? target[bone].x, target[bone].x, smoothing),
            y: lerp(smoothedLiveBones[bone]?.y ?? target[bone].y, target[bone].y, smoothing),
            z: lerp(smoothedLiveBones[bone]?.z ?? target[bone].z, target[bone].z, smoothing),
        };
    }
    smoothedLiveBones = out;
    return out;
}

function applyBoneFrame(vrm, frame) {
    if (!frame) return;
    for (const [boneName, rot] of Object.entries(frame)) {
        const bone = getBone(vrm, boneName);
        if (bone) {
            bone.rotation.x = rot.x;
            bone.rotation.y = rot.y;
            bone.rotation.z = rot.z;
        }
    }
}

// ═══════════════════════════════════════════
// RECORDED MOCAP PLAYBACK (fallback when webcam is off)
// ═══════════════════════════════════════════
let mocapData = null;
let mocapDuration = 0;
let useMocap = false;

async function loadMocap() {
    try {
        const resp = await fetch('/animations/idle_mocap.json');
        if (!resp.ok) throw new Error('No mocap file');
        mocapData = await resp.json();
        mocapDuration = mocapData[mocapData.length - 1].t;
        useMocap = true;
        console.log(`Mocap loaded: ${mocapData.length} frames, ${mocapDuration.toFixed(1)}s`);
    } catch (e) {
        console.log('No mocap file found, using drift idle.');
        useMocap = false;
    }
    // Start live tracking after mocap fallback is ready either way
    setupLiveTracking();
}

function getMocapFrame(t) {
    if (!mocapData || mocapData.length < 2) return null;
    const loopT = t % mocapDuration;
    let i = 0;
    for (; i < mocapData.length - 1; i++) {
        if (mocapData[i + 1].t > loopT) break;
    }
    const a = mocapData[i];
    const b = mocapData[Math.min(i + 1, mocapData.length - 1)];
    const dt = b.t - a.t;
    const frac = dt > 0 ? (loopT - a.t) / dt : 0;
    const result = {};
    for (const bone of Object.keys(a.bones)) {
        result[bone] = {
            x: lerp(a.bones[bone].x, b.bones[bone].x, frac),
            y: lerp(a.bones[bone].y, b.bones[bone].y, frac),
            z: lerp(a.bones[bone].z, b.bones[bone].z, frac),
        };
    }
    return result;
}

// ═══════════════════════════════════════════
// DRIFT SYSTEM — last-resort fallback (no webcam, no mocap)
// ═══════════════════════════════════════════
function createDrift(minVal, maxVal, minHold, maxHold, speed) {
    return {
        current: 0, target: randRange(minVal, maxVal), holdUntil: 0,
        minVal, maxVal, minHold, maxHold, speed
    };
}
function updateDrift(d, t) {
    if (t > d.holdUntil && Math.abs(d.current - d.target) < 0.001) {
        d.target = randRange(d.minVal, d.maxVal);
        d.holdUntil = t + randRange(d.minHold, d.maxHold);
    }
    d.current = lerp(d.current, d.target, d.speed);
    return d.current;
}

const drifts = {
    headX: createDrift(-0.02, 0.03, 2, 6, 0.008),
    headY: createDrift(-0.03, 0.03, 3, 8, 0.006),
    headZ: createDrift(-0.01, 0.01, 4, 10, 0.005),
    neckX: createDrift(-0.01, 0.01, 3, 8, 0.005),
    neckY: createDrift(-0.015, 0.015, 4, 10, 0.004),
    spineX: createDrift(-0.005, 0.008, 4, 12, 0.003),
    hipsZ: createDrift(-0.008, 0.008, 5, 15, 0.003),
    hipsY: createDrift(-0.005, 0.005, 6, 15, 0.002),
};

// ═══════════════════════════════════════════
// WEBSOCKET — connects to Lily.py
// ═══════════════════════════════════════════
const lilyState = {
    talking: false, talkAmplitude: 0.7,
    emotion: 'neutral', emotionUntil: 0, connected: false,
};

let ws = null;
function connectWS() {
    try {
        ws = new WebSocket('ws://127.0.0.1:8765');
        ws.onopen = () => { lilyState.connected = true; console.log('[WS] Connected'); };
        ws.onmessage = (e) => {
            try {
                const msg = JSON.parse(e.data);
                if (msg.type === 'talk_start') lilyState.talking = true;
                else if (msg.type === 'talk_stop') lilyState.talking = false;
                else if (msg.type === 'talk_amp') lilyState.talkAmplitude = msg.value ?? 0.7;
                else if (msg.type === 'emotion') {
                    lilyState.emotion = msg.name ?? 'neutral';
                    lilyState.emotionUntil = performance.now() / 1000 + (msg.duration ?? 5);
                }
            } catch (err) { }
        };
        ws.onclose = () => { lilyState.connected = false; setTimeout(connectWS, 3000); };
        ws.onerror = () => ws.close();
    } catch (e) { setTimeout(connectWS, 3000); }
}
connectWS();

// ═══════════════════════════════════════════
// MOUSE TRACKING (used only as fallback when live webcam tracking is off)
// ═══════════════════════════════════════════
const mouse = { x: 0, y: 0, active: false, lastMove: 0 };
const lookTarget = { x: 0, y: 0 };

window.addEventListener('mousemove', (e) => {
    mouse.x = (e.clientX / window.innerWidth) * 2 - 1;
    mouse.y = -(e.clientY / window.innerHeight) * 2 + 1;
    mouse.active = true;
    mouse.lastMove = performance.now() / 1000;
});

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
// BLINK
// ═══════════════════════════════════════════
const blinkState = { next: 2, active: false, timer: 0, dur: 0.12, double: false };
function updateBlink(vrm, dt) {
    const expr = vrm.expressionManager;
    if (!expr) return;
    if (!blinkState.active) {
        blinkState.next -= dt;
        if (blinkState.next <= 0) {
            blinkState.active = true; blinkState.timer = 0;
            blinkState.double = Math.random() < 0.12;
            blinkState.next = 2.5 + Math.random() * 5;
        }
        expr.setValue('blink', 0);
    } else {
        blinkState.timer += dt;
        const p = blinkState.timer / blinkState.dur;
        if (p < 0.3) expr.setValue('blink', p / 0.3);
        else if (p < 0.7) expr.setValue('blink', 1.0);
        else if (p < 1.0) expr.setValue('blink', 1 - (p - 0.7) / 0.3);
        else {
            expr.setValue('blink', 0);
            if (blinkState.double) { blinkState.double = false; blinkState.timer = 0; }
            else blinkState.active = false;
        }
    }
}

// ═══════════════════════════════════════════
// LIP SYNC (TTS-driven — kept exactly as-is, untouched by body tracking)
// ═══════════════════════════════════════════
const visemes = ['aa', 'ih', 'ou', 'ee', 'oh'];
const visemeWeights = [0.40, 0.15, 0.15, 0.20, 0.10];
let currentViseme = 'aa', nextVisemeChange = 0, smoothedMouthAmp = 0;

function updateLipSync(vrm, t) {
    const expr = vrm.expressionManager;
    if (!expr) return;
    if (!lilyState.talking) {
        smoothedMouthAmp = lerp(smoothedMouthAmp, 0, 0.15);
        for (const v of visemes) { try { expr.setValue(v, 0); } catch (e) { } }
        return;
    }
    if (t >= nextVisemeChange) {
        let r = Math.random(), sum = 0;
        for (let i = 0; i < visemes.length; i++) {
            sum += visemeWeights[i];
            if (r <= sum) { currentViseme = visemes[i]; break; }
        }
        nextVisemeChange = t + 0.08 + Math.random() * 0.08;
    }
    smoothedMouthAmp = lerp(smoothedMouthAmp, lilyState.talkAmplitude, 0.35);
    for (const v of visemes) {
        try { expr.setValue(v, v === currentViseme ? Math.min(0.7, smoothedMouthAmp * 0.65) : 0); } catch (e) { }
    }
}

// ═══════════════════════════════════════════
// EMOTION EXPRESSIONS
// ═══════════════════════════════════════════
const emotionBlends = {
    neutral: {}, happy: { happy: 0.5 }, sad: { sad: 0.4 },
    angry: { angry: 0.4 }, surprised: { surprised: 0.6 },
    shy: { happy: 0.15 }, sleepy: { relaxed: 0.3 }, excited: { happy: 0.7 },
};

function updateEmotionExpressions(vrm) {
    const expr = vrm.expressionManager;
    if (!expr) return;
    const now = performance.now() / 1000;
    const active = (now < lilyState.emotionUntil) ? lilyState.emotion : 'neutral';
    const blends = emotionBlends[active] || {};
    for (const name of ['happy', 'sad', 'angry', 'surprised', 'relaxed']) {
        try {
            const cur = expr.getValue(name) || 0;
            expr.setValue(name, lerp(cur, blends[name] || 0, 0.03));
        } catch (e) { }
    }
}

// ═══════════════════════════════════════════
// DRIFT IDLE (last-resort fallback)
// ═══════════════════════════════════════════
function getBreath(t) {
    return Math.sin(t * 1.6) * 0.004 + (Math.sin(t * 0.5) > 0.9 ? Math.sin(t * 1.6) * 0.003 : 0);
}

function updateIdleDrift(vrm, t) {
    const breath = getBreath(t);
    const spine = getBone(vrm, 'spine');
    const chest = getBone(vrm, 'chest');
    if (spine) spine.rotation.x = breath + updateDrift(drifts.spineX, t);
    if (chest) chest.rotation.x = breath * 0.5;

    const hips = getBone(vrm, 'hips');
    if (hips) {
        hips.rotation.z = updateDrift(drifts.hipsZ, t);
        hips.rotation.y = updateDrift(drifts.hipsY, t);
    }

    const head = getBone(vrm, 'head');
    const neck = getBone(vrm, 'neck');
    if (neck) {
        neck.rotation.x = updateDrift(drifts.neckX, t);
        neck.rotation.y = updateDrift(drifts.neckY, t) + lookTarget.x * 0.3;
    }
    if (head) {
        head.rotation.x = updateDrift(drifts.headX, t) - lookTarget.y * 0.15;
        head.rotation.y = updateDrift(drifts.headY, t) + lookTarget.x * 0.5;
        head.rotation.z = updateDrift(drifts.headZ, t);
    }

    const lua = getBone(vrm, 'leftUpperArm');
    const rua = getBone(vrm, 'rightUpperArm');
    const lla = getBone(vrm, 'leftLowerArm');
    const rla = getBone(vrm, 'rightLowerArm');
    if (lua) { lua.rotation.z = 0.75; lua.rotation.x = 0; }
    if (rua) { rua.rotation.z = -0.75; rua.rotation.x = 0; }
    if (lla) lla.rotation.y = -0.12;
    if (rla) rla.rotation.y = 0.12;

    const lh = getBone(vrm, 'leftHand');
    const rh = getBone(vrm, 'rightHand');
    if (lh) lh.rotation.x = 0.08;
    if (rh) rh.rotation.x = 0.08;

    const ls = getBone(vrm, 'leftShoulder');
    const rs = getBone(vrm, 'rightShoulder');
    if (ls) ls.rotation.z = breath * 0.3;
    if (rs) rs.rotation.z = -breath * 0.3;
}

// ═══════════════════════════════════════════
// MAIN BODY UPDATE
// Priority: live webcam tracking > recorded mocap loop > drift fallback
// Head rotation: driven by webcam when live tracking is active,
// otherwise falls back to mouse-look.
// ═══════════════════════════════════════════
function updateIdle(vrm, t, dt) {
    if (liveTrackingActive && liveLandmarks) {
        const rawBones = landmarksToBoneRotations(liveLandmarks);
        const bones = smoothLiveBones(rawBones, 0.35);
        applyBoneFrame(vrm, bones);

        // Layer talk head bob on top of your real head motion
        if (lilyState.talking) {
            const head = getBone(vrm, 'head');
            if (head) {
                head.rotation.x += Math.sin(t * 1.5) * 0.012 * lilyState.talkAmplitude;
                head.rotation.z += Math.sin(t * 4) * 0.008 * lilyState.talkAmplitude;
            }
        }
    } else if (useMocap) {
        const frame = getMocapFrame(t);
        applyBoneFrame(vrm, frame);

        const head = getBone(vrm, 'head');
        if (head) {
            head.rotation.x -= lookTarget.y * 0.15;
            head.rotation.y += lookTarget.x * 0.5;
        }
        if (lilyState.talking && head) {
            head.rotation.x += Math.sin(t * 1.5) * 0.012 * lilyState.talkAmplitude;
            head.rotation.z += Math.sin(t * 4) * 0.008 * lilyState.talkAmplitude;
        }
    } else {
        updateIdleDrift(vrm, t);
    }
}

// ═══════════════════════════════════════════
// ANIMATION LOOP
// ═══════════════════════════════════════════
const clock = new THREE.Clock();
function animate() {
    requestAnimationFrame(animate);
    const dt = clock.getDelta();
    const t = clock.getElapsedTime();

    if (currentVrm) {
        updateMouseLook(t);
        updateIdle(currentVrm, t, dt);
        updateBlink(currentVrm, dt);
        updateLipSync(currentVrm, t);
        updateEmotionExpressions(currentVrm);
        currentVrm.update(dt);
    }
    renderer.render(scene, camera);
}
animate();

window.addEventListener('wheel', (e) => {
    camera.position.z += e.deltaY * 0.005;
    camera.position.z = Math.max(1.5, Math.min(6.0, camera.position.z));
});
window.addEventListener('resize', () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
});