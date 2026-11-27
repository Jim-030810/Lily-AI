"""
ws_animation_server.py
══════════════════════════════════════════════════════════════════════════
WebSocket server (port 8765) bridging Lily.py ↔ Three.js VRM viewer.
Now also includes a VMC protocol receiver (port 39540) that catches
bone/blendshape data from XR Animator and forwards it to the browser.

# ── Loading-progress cache ──
# notify_loading_progress()/notify_loading_complete() can (and routinely
# do, on a cold launch_lily.bat start) fire before the Electron/Vite
# viewer has even finished booting and opened its WebSocket — Vite +
# Electron startup races the TTS/Ollama/whisper startup steps, and the
# TTS step alone is the single biggest weight in the bar. _broadcast()
# only reaches clients connected *right now*, so every progress update
# sent before the viewer connects was previously just discarded, and the
# viewer would connect late to a stuck/empty bar (or nothing at all if
# loading_complete had already fired by then). Cache the last state and
# replay it to any client immediately on connect.
_last_loading_state = {"percent": 0, "label": None, "complete": False}

The viewer (main.js) expects these JSON messages:
  { "type": "talk_start" }
  { "type": "talk_stop"  }
  { "type": "talk_amp",  "value": 0.0–1.0 }
  { "type": "emotion",   "name": "<state>", "duration": <seconds> }
  { "type": "vmc_frame", "bones": {...}, "blendshapes": {...}, "root": {...} }
  { "type": "chat_message", "speaker": "lily"|"user", "text": "..." }

Public API (all thread-safe, called from Lily.py):
  notify_talk_start()
  notify_talk_stop()
  notify_talk_amp(value)
  notify_emotion(name, duration)
  notify_chat_message(speaker, text)
  await speak_with_lipsync(audio_bytes)
  is_vmc_receiving()  — True if XR Animator is actively sending data

Boot once from Lily.py:
  asyncio.create_task(ws_server.start_server())

══════════════════════════════════════════════════════════════════════════
FIXES APPLIED (see inline comments marked "FIX:"):
  1. websockets.serve() now sets max_size so image-bearing chat_input
     messages (base64 photos can easily be 2-5MB) aren't silently
     dropped by the library with an auto-disconnect before _handler
     ever runs. This was almost certainly the "keeps disconnecting /
     never replies to image inputs" bug.
  2. _invoke_chat_callback now logs a clear warning when an image was
     sent but the registered callback's signature has no `image`
     parameter, so a missing-image bug in Lily.py is now visible in
     the console instead of silently swallowed.
  3. MOTION SMOOTHING: raw VMC bone rotations and face blendshapes were
     being forwarded to the browser exactly as they arrived from XR
     Animator, frame by frame, with zero filtering. Mocap data is noisy
     at the joint level, and with no smoothing that noise shows up as
     visible micro-jitter/twitchiness once it drives a full VRM rig —
     the classic "robotic" look. Real vtuber apps (VSeeFace, VTube
     Studio, etc.) always low-pass filter incoming tracking data before
     applying it. We now do the same: nlerp smoothing on bone
     quaternions + root position/rotation, and exponential smoothing on
     blendshapes. See BONE_SMOOTHING / BLEND_SMOOTHING below to tune.
  4. STALE-BONE REST RETURN: if a tracked bone stops sending updates
     (hand leaves frame, tracking loss, etc.) it used to freeze at its
     last rotation forever — you'd get a stuck/floating limb while the
     rest of the body kept moving. Stale bones now ease back toward a
     neutral rest pose instead of staying frozen.
  5. EMOTION DEBOUNCE: the auto-expression detector used to commit to
     a new expression the instant any blendshape crossed threshold for
     a single frame, so tracking noise could flicker the face between
     neutral and an expression. It now requires the winning expression
     to be sustained for EMOTION_HOLD_TIME before committing.
  6. OUTLIER REJECTION (bone rotation): monocular webcam pose estimation
     (XR Animator) periodically mis-estimates shoulder/torso rotation
     for a frame or two — this is the actual cause of a rig showing
     periodic arm abduction/adduction "pops" and diagonal torso twists
     every few seconds, and a plain low-pass filter alone can't remove
     it. A bone rotation that jumps further in one update than is
     physically plausible for a real person is now mostly rejected
     instead of blended in (see MAX_BONE_ANGLE_STEP_DEG).
  8. ROOT CALIBRATION: the avatar could appear permanently rotated
     onto a diagonal axis — not jittering, just constantly tilted.
     That's a baseline error (tracking's "zero" orientation wasn't
     square to the camera when it started), not noise, so smoothing/
     outlier rejection can't fix it. The first Hips frame seen (or the
     first one after a real tracking gap) is now captured as a "zero"
     reference and every later frame is rotated by its inverse, so
     motion is always relative to wherever she started. A "recalibrate"
     WS message can also force a manual re-zero.
  7. Fixed a bug in fix #4 itself: the stale-bone "ease back to rest"
     timeout was originally 0.4s, shorter than XR Animator's normal gap
     between updates for a bone that isn't currently moving. That
     falsely treated an ordinary pause as tracking loss, eased the bone
     toward rest, then visibly snapped it back once real data resumed —
     which likely was the periodic twitch being reported. Timeout is
     now 1.2s, and a bone recovering from genuine staleness snaps
     straight to its true value instead of blending in from rest.
══════════════════════════════════════════════════════════════════════════
"""

import asyncio
import inspect
import io
import json
import math
import threading
import time
import traceback
from typing import Optional, Set, cast
import yaml
import numpy as np
import sounddevice as sd

try:
    import websockets
    # Import the factory directly so type checkers do not need to resolve
    # the version-dependent ``websockets.serve`` module attribute.
    try:
        from websockets.asyncio.server import serve as websocket_serve
    except ImportError:  # websockets < 14
        from websockets.server import serve as websocket_serve
    from websockets.exceptions import ConnectionClosed
except ImportError:
    websockets = None
    websocket_serve = None
    ConnectionClosed = ()
    print("[ws_server] ⚠️  'websockets' not installed.  pip install websockets")

try:
    from pydub import AudioSegment
except ImportError:
    AudioSegment = None

try:
    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_server import AsyncIOOSCUDPServer
    _HAS_OSC = True
except ImportError:
    _HAS_OSC = False
    print("[ws_server] ⚠️  'python-osc' not installed. VMC receiving disabled.")

# ── Config ─────────────────────────────────────────────────────────────
# Load from config.yaml in the same directory as this script
_config_path = "config.yaml"
try:
    with open(_config_path, "r", encoding="utf-8") as f:
        _config = yaml.safe_load(f)
    if not isinstance(_config, dict):
        raise ValueError("config.yaml root must be a mapping")
    _ws_config = _config.get("ws_server", {})
    if not isinstance(_ws_config, dict):
        raise ValueError("ws_server config must be a mapping")
    WS_HOST = _ws_config.get("host", "127.0.0.1")
    WS_PORT = _ws_config.get("port", 8765)
except FileNotFoundError:
    print(f"[ws_server] ⚠️  {_config_path} not found, using defaults")
    WS_HOST = "127.0.0.1"
    WS_PORT = 8765
except Exception as e:
    print(f"[ws_server] ⚠️  Failed to load config: {e}, using defaults")
    WS_HOST = "127.0.0.1"
    WS_PORT = 8765

# FIX #3: there used to be a second "Config" block here that
# unconditionally set WS_HOST = "127.0.0.1", overwriting whatever
# config.yaml said one paragraph above. That meant the server could
# NEVER be reached from another device (like a phone) on the LAN no
# matter what config.yaml said. That block is removed — WS_HOST/WS_PORT
# now keep whatever config.yaml loaded above. To let your phone connect
# over WiFi, set in config.yaml:
#   ws_server:
#     host: 0.0.0.0
#     port: 8765
AMP_UPDATE_HZ = 25          # how often we send talk_amp while speaking

VMC_RECV_PORT = 40000        # XR Animator sends here (set this in XR Animator)
VMC_SEND_HZ = 60             # max frames/sec forwarded to browser
VMC_TIMEOUT = 2.0            # seconds without data before considered inactive

# FIX #1: websockets' default max_size is 1 MiB. Base64-encoded phone
# photos routinely blow past that, and the library silently closes the
# connection (code 1009, "message too big") before _handler ever sees
# the frame. Raise it to 25MB to comfortably cover camera photos.
WS_MAX_MESSAGE_SIZE = 25 * 1024 * 1024

# ── FIX #3: Motion smoothing config ────────────────────────────────────
# Tune these if it still looks off:
#   - Movement feels laggy/delayed  -> lower the SMOOTHING values
#   - Still looks twitchy/jittery   -> raise the SMOOTHING values
# 0.0 = no smoothing at all (raw passthrough), close to 1.0 = frozen.
BONE_SMOOTHING = 0.35        # limb/body rotation smoothing
ROOT_POS_SMOOTHING = 0.2     # hips position (translation) — kept lighter
                              # than rotation so walking doesn't feel laggy
BLEND_SMOOTHING = 0.45       # face blendshapes can smooth more without
                              # reading as "delayed" the way body motion would

BONE_STALE_TIMEOUT = 1.2          # seconds without an update before a bone
                                    # is considered "lost tracking". Kept
                                    # comfortably longer than normal XR
                                    # Animator send gaps — too short (an
                                    # earlier version used 0.4s) falsely
                                    # treats an ordinary pause between
                                    # updates as tracking loss, eases the
                                    # bone toward rest, then visibly
                                    # twitches it back once real data
                                    # resumes. That was likely the cause
                                    # of the periodic arm/torso "pop."
BONE_RETURN_TO_REST_ALPHA = 0.92   # per-tick ease factor back to rest pose
                                    # for stale bones (closer to 1 = slower)

# Outlier rejection for single/couple-frame tracking glitches. Monocular
# webcam pose estimation (what XR Animator does) is notoriously prone to
# brief mis-estimates specifically on shoulder rotation and torso twist —
# depth is ambiguous from one camera, so the solver occasionally guesses
# a wrong-but-plausible rotation for a frame or two before correcting
# itself. That shows up exactly as "twitchy abduction/adduction" and
# sudden diagonal torso snaps recurring every few seconds. A plain
# low-pass filter can't fully remove this without also making real
# motion laggy, so instead: if a bone's rotation jumps further in one
# update than is physically plausible for a real human at this send
# rate, treat it as a probable glitch and barely let it through.
MAX_BONE_ANGLE_STEP_DEG = 35.0    # a rotation jump bigger than this in a
                                    # single update is treated as a likely
                                    # tracking glitch rather than real motion
OUTLIER_BLEND_ALPHA = 0.92         # how much weight the OLD value keeps
                                    # when a frame is flagged as a glitch

EMOTION_HOLD_TIME = 0.25     # seconds an expression must stay dominant
                              # before we commit to it (prevents flicker)

_IDENTITY_QUAT = {"qx": 0.0, "qy": 0.0, "qz": 0.0, "qw": 1.0}

# ── Root calibration ────────────────────────────────────────────────
# XR Animator's raw Hips rotation is measured relative to the camera/
# room, not to "standing upright and square to the camera". If the
# tracked person wasn't perfectly square when tracking (re)started,
# that tilt gets baked into every frame from then on as a constant
# offset — the avatar reads as permanently rotated onto a diagonal
# axis. This is a baseline error, not noise, so smoothing and outlier
# rejection (see MAX_BONE_ANGLE_STEP_DEG) can't fix it; they only
# handle frame-to-frame jitter around whatever the baseline is.
# Fix: capture the first Hips rotation seen as a "zero" reference and
# rotate every later frame by its inverse, so motion is always
# expressed relative to wherever she started, regardless of which way
# that actually faced the camera.
_root_calibration = None  # inverse of the reference ("zero") Hips rotation


def _quat_inverse(q: dict) -> dict:
    return {"qx": -q["qx"], "qy": -q["qy"], "qz": -q["qz"], "qw": q["qw"]}


def _quat_mul(a: dict, b: dict) -> dict:
    """Hamilton product a*b (apply rotation a, then b's frame... here
    used as: rotate raw_q by the calibration offset)."""
    return {
        "qx": a["qw"]*b["qx"] + a["qx"]*b["qw"] + a["qy"]*b["qz"] - a["qz"]*b["qy"],
        "qy": a["qw"]*b["qy"] - a["qx"]*b["qz"] + a["qy"]*b["qw"] + a["qz"]*b["qx"],
        "qz": a["qw"]*b["qz"] + a["qx"]*b["qy"] - a["qy"]*b["qx"] + a["qz"]*b["qw"],
        "qw": a["qw"]*b["qw"] - a["qx"]*b["qx"] - a["qy"]*b["qy"] - a["qz"]*b["qz"],
    }


def recalibrate_root():
    """Force the next Hips frame received to become the new upright
    reference. Called automatically whenever tracking resumes after a
    real gap (see _on_bone), and can also be triggered manually — e.g.
    wire a 'recalibrate' WS message from the viewer to this, so the
    streamer can re-zero by just standing square and pressing a key."""
    global _root_calibration
    _root_calibration = None

# ── Unity HumanBodyBones → VRM bone name mapping ──
BONE_MAP = {
    "Hips": "hips", "Spine": "spine", "Chest": "chest",
    "UpperChest": "upperChest", "Neck": "neck", "Head": "head",
    "LeftShoulder": "leftShoulder", "LeftUpperArm": "leftUpperArm",
    "LeftLowerArm": "leftLowerArm", "LeftHand": "leftHand",
    "RightShoulder": "rightShoulder", "RightUpperArm": "rightUpperArm",
    "RightLowerArm": "rightLowerArm", "RightHand": "rightHand",
    "LeftUpperLeg": "leftUpperLeg", "LeftLowerLeg": "leftLowerLeg",
    "LeftFoot": "leftFoot", "LeftToes": "leftToes",
    "RightUpperLeg": "rightUpperLeg", "RightLowerLeg": "rightLowerLeg",
    "RightFoot": "rightFoot", "RightToes": "rightToes",
    "LeftEye": "leftEye", "RightEye": "rightEye", "Jaw": "jaw",
    # Fingers
    "LeftThumbProximal": "leftThumbMetacarpal",
    "LeftThumbIntermediate": "leftThumbProximal",
    "LeftThumbDistal": "leftThumbDistal",
    "LeftIndexProximal": "leftIndexProximal",
    "LeftIndexIntermediate": "leftIndexIntermediate",
    "LeftIndexDistal": "leftIndexDistal",
    "LeftMiddleProximal": "leftMiddleProximal",
    "LeftMiddleIntermediate": "leftMiddleIntermediate",
    "LeftMiddleDistal": "leftMiddleDistal",
    "LeftRingProximal": "leftRingProximal",
    "LeftRingIntermediate": "leftRingIntermediate",
    "LeftRingDistal": "leftRingDistal",
    "LeftLittleProximal": "leftLittleProximal",
    "LeftLittleIntermediate": "leftLittleIntermediate",
    "LeftLittleDistal": "leftLittleDistal",
    "RightThumbProximal": "rightThumbMetacarpal",
    "RightThumbIntermediate": "rightThumbProximal",
    "RightThumbDistal": "rightThumbDistal",
    "RightIndexProximal": "rightIndexProximal",
    "RightIndexIntermediate": "rightIndexIntermediate",
    "RightIndexDistal": "rightIndexDistal",
    "RightMiddleProximal": "rightMiddleProximal",
    "RightMiddleIntermediate": "rightMiddleIntermediate",
    "RightMiddleDistal": "rightMiddleDistal",
    "RightRingProximal": "rightRingProximal",
    "RightRingIntermediate": "rightRingIntermediate",
    "RightRingDistal": "rightRingDistal",
    "RightLittleProximal": "rightLittleProximal",
    "RightLittleIntermediate": "rightLittleIntermediate",
    "RightLittleDistal": "rightLittleDistal",
}
server_ready = asyncio.Event()
# ── Internal state ─────────────────────────────────────────────────────
_clients: Set = set()
_loop: Optional[asyncio.AbstractEventLoop] = None
_shutdown_callback = None
# Cached loading state replayed to clients that connect after progress
# updates have already been emitted.
_last_loading_state = {"percent": 0, "label": None, "complete": False}

# VMC receiver state
_vmc_bones = {}           # { vrm_bone_name: { qx, qy, qz, qw } }  (smoothed)
_vmc_blendshapes = {}     # { name: value }  (smoothed)
_vmc_root = None          # { px, py, pz, qx, qy, qz, qw } from Hips position (smoothed)
_vmc_last_time = 0.0      # last time we got VMC data
_vmc_dirty = False        # new data since last broadcast
_vmc_bone_last_seen = {}  # { vrm_bone_name: timestamp } — for stale-bone rest return

# ── Emotion detection from VMC blendshapes ─────────────────────────────
_BLEND_TO_EMOTION = {
    "Angry":     "angry",
    "BrowDown":  "angry",
    "Sad":       "sad",
    "Surprised": "surprised",
    "BrowUp":    "surprised",
    "Joy":       "happy",
    "Smile":     "happy",
    "Smug":      "smug",
    "Blush":     "shy",
    "Sweat":     "nervous",
}
_last_vmc_emotion = "neutral"
_vmc_emotion_cooldown = 0.0
_pending_emotion = None       # FIX #5: candidate emotion waiting to be confirmed
_pending_emotion_since = 0.0

_chat_input_callback = None

def set_chat_input_callback(cb):
    global _chat_input_callback
    _chat_input_callback = cb

def set_shutdown_callback(cb):
    global _shutdown_callback
    _shutdown_callback = cb


def is_vmc_receiving() -> bool:
    """Returns True if XR Animator is actively sending VMC data."""
    return (time.time() - _vmc_last_time) < VMC_TIMEOUT


# ── FIX #3: quaternion / value smoothing helpers ───────────────────────
def _nlerp_quat(prev: dict, new: dict, alpha: float) -> dict:
    """Cheap normalized-lerp smoothing between two quaternions.

    Good enough at the 60Hz-ish update rates VMC runs at, where the
    delta between consecutive frames is small — a full slerp isn't
    worth the extra trig here. `alpha` is how much weight stays on the
    *previous* (already-smoothed) value; higher = smoother but laggier.

    Also guards against quaternion double-cover flips (q and -q encode
    the same rotation but interpolating between them naively causes a
    visible snap/spasm), by flipping the new quaternion's sign to match
    the previous one whenever their dot product goes negative.
    """
    dot = (prev["qx"] * new["qx"] + prev["qy"] * new["qy"]
           + prev["qz"] * new["qz"] + prev["qw"] * new["qw"])
    sign = -1.0 if dot < 0 else 1.0

    qx = prev["qx"] * alpha + new["qx"] * sign * (1 - alpha)
    qy = prev["qy"] * alpha + new["qy"] * sign * (1 - alpha)
    qz = prev["qz"] * alpha + new["qz"] * sign * (1 - alpha)
    qw = prev["qw"] * alpha + new["qw"] * sign * (1 - alpha)

    mag = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw) or 1.0
    return {
        "qx": round(qx / mag, 5), "qy": round(qy / mag, 5),
        "qz": round(qz / mag, 5), "qw": round(qw / mag, 5),
    }


def _smooth_scalar(prev: Optional[float], new: float, alpha: float) -> float:
    if prev is None:
        return new
    return prev * alpha + new * (1 - alpha)


def _quat_angle_deg(a: dict, b: dict) -> float:
    """Angle (degrees) between two rotations, used to flag implausibly
    large single-frame jumps as tracking glitches rather than real
    motion."""
    dot = abs(a["qx"] * b["qx"] + a["qy"] * b["qy"]
              + a["qz"] * b["qz"] + a["qw"] * b["qw"])
    dot = min(1.0, max(-1.0, dot))
    return math.degrees(2 * math.acos(dot))


def _smooth_rotation(prev: Optional[dict], raw_q: dict, was_stale: bool) -> dict:
    """Smooth one bone/root rotation update.

    - First time we've ever seen this bone, or it's recovering after a
      genuine tracking-loss gap (was_stale): snap straight to the raw
      value. Blending in from a rest/eased pose here is what produced
      a visible "twitch" once tracking resumed.
    - A single-frame rotation jump bigger than MAX_BONE_ANGLE_STEP_DEG
      is treated as a probable pose-estimation glitch (see comment on
      that constant) and mostly rejected rather than blended in.
    - Otherwise: normal nlerp smoothing.
    """
    if prev is None or was_stale:
        return {k: round(v, 5) for k, v in raw_q.items()}
    angle = _quat_angle_deg(prev, raw_q)
    alpha = OUTLIER_BLEND_ALPHA if angle > MAX_BONE_ANGLE_STEP_DEG else BONE_SMOOTHING
    return _nlerp_quat(prev, raw_q, alpha)


# ── Broadcast helpers ──────────────────────────────────────────────────
def _payload(**kw) -> str:
    return json.dumps(kw)


async def _broadcast(data: str):
    if not _clients:
        return
    dead = set()
    for ws in list(_clients):
        try:
            await ws.send(data)
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)


def _fire(data: str):
    if _loop and not _loop.is_closed():
        asyncio.run_coroutine_threadsafe(_broadcast(data), _loop)


# ── Public sync API (thread-safe) ──────────────────────────────────────
def notify_talk_start():
    _fire(_payload(type="talk_start"))


def notify_talk_stop():
    _fire(_payload(type="talk_stop"))


def notify_talk_amp(value: float):
    _fire(_payload(type="talk_amp", value=round(float(value), 3)))


def notify_emotion(name: str, duration: float = 12.0):
    _fire(_payload(type="emotion", name=name, duration=duration))

def notify_mode(mode: str):
    """Tell the browser which mode we're in so it can show/hide the text bar."""
    _fire(_payload(type="mode_change", mode=mode))

def notify_coding_context(active: bool, summary: str = ""):
    """Tells any connected viewer client whether Lily currently has
    live coding context loaded (see extract_coding_context in
    lily_bot.py), so the front-end can light up/clear the coding
    assistant badge accordingly."""
    _fire(_payload(type="coding_context", active=active, summary=summary))

def notify_loading_progress(percent: int, label: str):
    """Drive the viewer's single startup progress bar/overlay."""
    _last_loading_state["percent"] = int(percent)
    _last_loading_state["label"] = label
    _last_loading_state["complete"] = False
    _fire(_payload(type="loading_progress", percent=int(percent), label=label))

def notify_lily_code(code: str, explanation: str = "", language: str = ""):
    """Pushes a code suggestion FROM LILY HERSELF into any connected
    coding-assistant window (codechat.js connects to this same WS
    server, same as lily-viewer.js does). Rendered there as a
    distinct blue-accented 'Lily' bubble, separate from the local
    Ollama assistant's own replies in that window, so it's visually
    obvious which suggestion came from her."""
    _fire(_payload(type="lily_code", code=code, explanation=explanation, language=language))

def notify_loading_complete():
    """Tell the browser startup is finished so it can fade out the
    loading overlay."""
    _last_loading_state["complete"] = True
    _fire(_payload(type="loading_complete"))

def notify_song_mood(mood: str, energy: float):
    """Tell the browser what mood/energy the currently playing song has,
    so it can bop/sway in a way that matches (hype/sad/chill). Send
    mood='none' to stop the bop entirely."""
    _fire(_payload(type="song_mood", mood=mood, energy=round(float(energy), 3)))
# ── VMC OSC handlers ──────────────────────────────────────────────────
def _on_bone(address, *args):
    """Receives one bone's rotation (and, for Hips, position too) per
    call from XR Animator. FIX #3: rotations are now nlerp-smoothed
    against the previously smoothed value instead of passed through
    raw, which is what was causing visibly jittery/robotic motion."""
    global _vmc_dirty, _vmc_last_time, _vmc_root
    if len(args) < 8:
        return
    unity_name = args[0]
    now = time.time()

    raw_q = {"qx": args[4], "qy": args[5], "qz": args[6], "qw": args[7]}

    # Capture root (Hips) position + rotation data for the viewer
    if unity_name == "Hips":
        global _root_calibration
        # If we've never calibrated, or tracking had gone quiet for a
        # while (XR Animator restarted, person walked back into frame
        # after being lost), treat this as a fresh session and take a
        # new zero-reference rather than applying a stale offset to
        # someone who may now be standing differently.
        if _root_calibration is None or (now - _vmc_last_time) > BONE_STALE_TIMEOUT:
            _root_calibration = _quat_inverse(raw_q)
        raw_q = _quat_mul(_root_calibration, raw_q)

        raw_px, raw_py, raw_pz = round(args[1], 5), round(args[2], 5), round(args[3], 5)
        root_last_seen = _vmc_bone_last_seen.get("hips")
        root_was_stale = (_vmc_root is not None and root_last_seen is not None
                           and (now - root_last_seen) > BONE_STALE_TIMEOUT)
        smoothed_q = _smooth_rotation(_vmc_root, raw_q, root_was_stale)
        if _vmc_root and not root_was_stale:
            _vmc_root = {
                "px": round(_smooth_scalar(_vmc_root["px"], raw_px, ROOT_POS_SMOOTHING), 5),
                "py": round(_smooth_scalar(_vmc_root["py"], raw_py, ROOT_POS_SMOOTHING), 5),
                "pz": round(_smooth_scalar(_vmc_root["pz"], raw_pz, ROOT_POS_SMOOTHING), 5),
                **smoothed_q,
            }
        else:
            # First sighting, or recovering from a real tracking-loss
            # gap — snap position through too, don't blend from stale.
            _vmc_root = {"px": raw_px, "py": raw_py, "pz": raw_pz, **smoothed_q}

    vrm_name = BONE_MAP.get(unity_name)
    if vrm_name is None:
        return

    prev = _vmc_bones.get(vrm_name)
    last_seen = _vmc_bone_last_seen.get(vrm_name)
    was_stale = prev is not None and last_seen is not None and (now - last_seen) > BONE_STALE_TIMEOUT
    _vmc_bones[vrm_name] = _smooth_rotation(prev, raw_q, was_stale)

    _vmc_bone_last_seen[vrm_name] = now
    _vmc_dirty = True
    _vmc_last_time = now


def _on_blendshape(address, *args):
    """FIX #3: blendshape values are now exponentially smoothed instead
    of passed through raw, so the face doesn't flicker/twitch on mocap
    noise (this also feeds the emotion detector, so smoothing it here
    helps that too)."""
    global _vmc_dirty, _vmc_last_time
    if len(args) < 2:
        return
    name, val = args[0], float(args[1])
    prev = _vmc_blendshapes.get(name)
    _vmc_blendshapes[name] = round(_smooth_scalar(prev, val, BLEND_SMOOTHING), 4)
    _vmc_dirty = True
    _vmc_last_time = time.time()


def _on_blendshape_apply(address, *args):
    pass  # we send on the broadcast tick


# ── VMC broadcast loop ────────────────────────────────────────────────
async def _vmc_broadcast_loop():
    """Forward VMC bone data to browser clients at VMC_SEND_HZ.
    Also detects emotion from blendshape data and syncs the viewer's
    facial expression to match the body pose.

    FIX #4: bones that stop receiving updates (tracking loss, hand out
    of frame, etc.) used to stay frozen at their last rotation forever,
    producing a stuck/floating limb while the rest of the avatar kept
    moving. They now ease back toward an identity (rest) rotation.

    FIX #5: the emotion picked from blendshapes must now stay dominant
    for EMOTION_HOLD_TIME before we actually switch the viewer's
    expression, instead of committing on a single noisy frame."""
    global _vmc_dirty, _last_vmc_emotion, _vmc_emotion_cooldown
    global _pending_emotion, _pending_emotion_since
    interval = 1.0 / VMC_SEND_HZ

    while True:
        now = time.time()

        # ── FIX #4: ease stale bones back toward rest pose ──
        for bname, last_seen in list(_vmc_bone_last_seen.items()):
            if now - last_seen <= BONE_STALE_TIMEOUT:
                continue
            cur = _vmc_bones.get(bname)
            if cur is None:
                continue
            eased = _nlerp_quat(cur, _IDENTITY_QUAT, BONE_RETURN_TO_REST_ALPHA)
            if eased != cur:
                _vmc_bones[bname] = eased
                _vmc_dirty = True
            # Close enough to rest — stop touching it every tick.
            if abs(eased["qw"] - 1.0) < 0.001:
                _vmc_bone_last_seen.pop(bname, None)

        if _vmc_dirty and _clients:

            payload = {
                "type": "vmc_frame",
                "bones": dict(_vmc_bones),
            }
            if _vmc_blendshapes:
                payload["blendshapes"] = dict(_vmc_blendshapes)
            if _vmc_root:
                payload["root"] = dict(_vmc_root)

            await _broadcast(json.dumps(payload))
            _vmc_dirty = False

            # ── Auto-detect emotion from VMC blendshapes ───────────
            if now > _vmc_emotion_cooldown:
                best_emotion = "neutral"
                best_val = 0.0
                for bname, bval in _vmc_blendshapes.items():
                    emo = _BLEND_TO_EMOTION.get(bname)
                    if emo and bval > best_val and bval > 0.2:
                        best_emotion = emo
                        best_val = bval

                if best_emotion == _last_vmc_emotion:
                    _pending_emotion = None
                else:
                    # FIX #5: require the candidate to hold for a beat
                    # before we commit, so noise can't flicker the face.
                    if best_emotion != _pending_emotion:
                        _pending_emotion = best_emotion
                        _pending_emotion_since = now
                    elif now - _pending_emotion_since >= EMOTION_HOLD_TIME:
                        _last_vmc_emotion = best_emotion
                        _pending_emotion = None
                        if best_emotion != "neutral":
                            _vmc_emotion_cooldown = now + 0.5
                            await _broadcast(json.dumps({
                                "type": "emotion",
                                "name": best_emotion,
                                "duration": 15
                            }))

        await asyncio.sleep(interval)

# ── Audio device helper ───────────────────────────────────────────────
def _find_vbcable_device() -> Optional[int]:
    try:
        for idx, dev in enumerate(sd.query_devices()):
            name = str(dev.get("name", "") if isinstance(dev, dict) else dev)
            ch = int(dev.get("max_output_channels", 0) if isinstance(dev, dict) else 1)
            if ch > 0 and any(n in name for n in
                              ("CABLE Input", "CABLE In", "VB-Audio Virtual Cable")):
                return idx
    except Exception:
        pass
    return None


# ── RMS → mouth amplitude ─────────────────────────────────────────────
def _rms_to_amp(rms: float, peak: float) -> float:
    if rms < 1e-5 or peak < 1e-4:
        return 0.0
    normalised = min(1.0, rms / max(peak, 1e-5))
    return min(1.0, math.log10(1 + normalised * 9) * 1.1)


# ── speak_with_lipsync ────────────────────────────────────────────────
async def speak_with_lipsync(audio_bytes: bytes, play_audio: bool = True):
    if not audio_bytes:
        return

    if AudioSegment is None:
        notify_talk_start()
        notify_talk_amp(0.6)
        if play_audio:
            await asyncio.to_thread(_play_raw, audio_bytes)
        else:
            await asyncio.sleep(1.0)
        notify_talk_amp(0.0)
        notify_talk_stop()
        return

    try:
        seg = AudioSegment.from_file(io.BytesIO(audio_bytes), format="wav")
    except Exception as e:
        print(f"[ws_server] decode error: {e}")
        return

    samples = np.array(seg.get_array_of_samples(), dtype=np.float32)
    if seg.channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    samples /= 32768.0
    sr = seg.frame_rate
    peak = float(np.percentile(np.abs(samples), 98)) or 1e-5

    interval = 1.0 / AMP_UPDATE_HZ
    chunk_size = int(sr * interval)
    n_chunks = max(1, len(samples) // chunk_size)
    chunk_rms = []
    for i in range(n_chunks):
        c = samples[i * chunk_size : (i + 1) * chunk_size]
        chunk_rms.append(float(np.sqrt(np.mean(c ** 2))) if len(c) else 0.0)

    notify_talk_start()

    play_done = asyncio.Event()
    loop = asyncio.get_running_loop()

    if play_audio:
        def _play():
            dev = _find_vbcable_device()
            sd.play(samples, sr, device=dev)
            sd.wait()
            loop.call_soon_threadsafe(play_done.set)

        threading.Thread(target=_play, daemon=True).start()

    for rms in chunk_rms:
        if play_audio and play_done.is_set():
            break
        amp = _rms_to_amp(rms, peak)
        notify_talk_amp(amp)
        await asyncio.sleep(interval)

    if play_audio:
        await play_done.wait()

    notify_talk_amp(0.0)
    notify_talk_stop()


def _play_raw(audio_bytes: bytes):
    if AudioSegment is None:
        return
    try:
        seg = AudioSegment.from_file(io.BytesIO(audio_bytes), format="wav")
        s = np.array(seg.get_array_of_samples(), dtype=np.float32) / 32768.0
        if seg.channels == 2:
            s = s.reshape(-1, 2).mean(axis=1)
        sd.play(s, seg.frame_rate, device=_find_vbcable_device())
        sd.wait()
    except Exception as e:
        print(f"[ws_server] raw playback error: {e}")


# ── Safe callback invocation ─────────────────────────────────────────
async def _invoke_chat_callback(text: str, image: Optional[str] = None):
    """Call _chat_input_callback safely, adapting to whatever
    signature Lily.py registered (1-arg or 2-arg / with image kwarg)."""
    cb = _chat_input_callback
    if cb is None:
        print(f"[ws_server] ⚠️ chat_input received but no callback registered: {text!r}")
        return

    is_coro = asyncio.iscoroutinefunction(cb)

    # Try passing image if the callback accepts it
    accepts_image = False
    try:
        sig = inspect.signature(cb)
        accepts_image = "image" in sig.parameters
    except (ValueError, TypeError):
        pass

    # FIX #2: if a photo came in but the registered callback has no
    # `image` param, it was being dropped with zero indication why.
    # Surface it loudly so this is diagnosable from the console instead
    # of looking like Lily "ignoring" the picture.
    if image and not accepts_image:
        print(
            "[ws_server] ⚠️ image received from viewer but the chat_input "
            "callback registered via set_chat_input_callback() has no "
            "'image' parameter — the image is being DROPPED. Add an "
            "`image=None` (or `image`) parameter to your Lily.py callback "
            "to receive it."
        )

    try:
        if accepts_image and image:
            if is_coro:
                await cb(text, image=image)
            else:
                cb(text, image=image)
        else:
            if is_coro:
                await cb(text)
            else:
                cb(text)
        print(f"[ws_server] ✅ chat_input dispatched: {text!r}")
    except Exception as e:
        print(f"[ws_server] ❌ chat_input callback error: {e}")
        traceback.print_exc()


# ── WebSocket server ──────────────────────────────────────────────────
async def _handler(websocket):
    _clients.add(websocket)
    print(f"[ws_server] Browser connected ({len(_clients)} clients)")

    # Replay whatever loading state we've already reached — this client
    # may be connecting late (Electron/Vite cold boot racing the
    # Ollama/TTS/whisper startup steps), so any loading_progress /
    # loading_complete messages already broadcast before it connected
    # were sent into an empty _clients set and lost. Without this, the
    # overlay is either stuck wherever it last was (nowhere, if it
    # connects before the first update) or misses loading_complete
    # entirely and never fades out.
    try:
        if _last_loading_state["label"] is not None:
            await websocket.send(_payload(
                type="loading_progress",
                percent=_last_loading_state["percent"],
                label=_last_loading_state["label"],
            ))
        if _last_loading_state["complete"]:
            await websocket.send(_payload(type="loading_complete"))
    except Exception as e:
        print(f"[ws_server] failed to replay loading state: {e}")

    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[ws_server] ⚠️ bad JSON from browser: {raw[:100]}")
                continue

            try:
                msg_type = msg.get("type")

                if msg_type == "shutdown":
                    print("[ws_server] 🛑 Shutdown requested from viewer")
                    await _broadcast(_payload(type="shutdown_ack"))
                    if _shutdown_callback:
                        if asyncio.iscoroutinefunction(_shutdown_callback):
                            await _shutdown_callback()
                        else:
                            _shutdown_callback()

                elif msg_type == "recalibrate":
                    # Lets the viewer offer a "stand square and press
                    # a button" reset if the auto-calibration ever
                    # picks a bad first frame (e.g. person was mid-
                    # motion when tracking connected).
                    recalibrate_root()
                    print("[ws_server] 🧭 Root recalibrated — next Hips frame becomes the new upright reference")

                elif msg_type == "chat_input":
                    text = msg.get("text", "").strip()
                    image = msg.get("image")  # base64 string or None
                    if text or image:
                        await _invoke_chat_callback(text, image)
                    else:
                        print("[ws_server] ⚠️ empty chat_input ignored")

                else:
                    print(f"[ws_server] unknown message type: {msg_type}")

            except Exception as e:
                print(f"[ws_server] ❌ handler error: {e}")
                traceback.print_exc()

    except Exception as e:
        if isinstance(e, ConnectionClosed):
            print(f"[ws_server] connection closed: {e}")
        else:
            print(f"[ws_server] ❌ connection error: {e}")
    finally:
        _clients.discard(websocket)
        print(f"[ws_server] Browser disconnected ({len(_clients)} clients)")


def notify_chat_message(speaker: str, text: str):
    """Send a chat message to the browser.  speaker = 'user' or 'lily'.
    main.js reads msg.speaker (not msg.role)."""
    _fire(_payload(type="chat_message", speaker=speaker, text=text))


async def start_server(host: str = WS_HOST, port: int = WS_PORT):
    """Start the WebSocket server + VMC receiver."""
    global _loop
    _loop = asyncio.get_running_loop()

    if websockets is None or websocket_serve is None:
        print("[ws_server] Cannot start — websockets package not installed.")
        return

    # ── Start VMC/OSC receiver ──
    if _HAS_OSC:
        try:
            disp = Dispatcher()
            disp.map("/VMC/Ext/Bone/Pos", _on_bone)
            disp.map("/VMC/Ext/Blend/Val", _on_blendshape)
            disp.map("/VMC/Ext/Blend/Apply", _on_blendshape_apply)

            osc_server = AsyncIOOSCUDPServer(
                ("0.0.0.0", VMC_RECV_PORT), disp, _loop # type: ignore
            )
            await osc_server.create_serve_endpoint()
            print(f"[ws_server] ✅ VMC receiver on UDP :{VMC_RECV_PORT} (set XR Animator to send here)")

            asyncio.create_task(_vmc_broadcast_loop())
        except Exception as e:
            print(f"[ws_server] ⚠️ VMC receiver failed: {e}")
    else:
        print("[ws_server] VMC receiver disabled (install python-osc to enable)")

    # ── Start WebSocket server ──
    # FIX #1: explicit max_size so image-bearing chat_input messages
    # aren't auto-closed by the library before _handler runs.
    async with websocket_serve(
        _handler, host, port, max_size=WS_MAX_MESSAGE_SIZE
    ):
       print(f"[ws_server] ✅ Listening on ws://{host}:{port} (max_size={WS_MAX_MESSAGE_SIZE // (1024*1024)}MB)")
       server_ready.set()
       await asyncio.Future()