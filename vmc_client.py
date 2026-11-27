"""
vmc_client.py

Drop-in replacement for VTubeStudioClient. Sends VMC protocol OSC
messages to your VRM model instead of talking to VTube Studio's
websocket API.

Includes FULL BODY idle animation (breathing, weight shift, arm sway,
gestures), periodic blinking, and subtle eye saccades so she feels
alive instead of a mannequin.
"""

import asyncio
import io
import math
import random
import time
from typing import Optional, Dict, List

import numpy as np
from pydub import AudioSegment
from pythonosc.udp_client import SimpleUDPClient
from scipy.spatial.transform import Rotation as R


# ============================================================
# BONE POSITIONS
# ============================================================

HEAD_POS = (
    -1.359684178809672e-10,
    0.05747377872467041,
    0.007213786244392395
)

SPINE_POS = (
    0.0,
    0.04311231772402344,
    0.010174106800651664
)

ZERO_POS = (0.0, 0.0, 0.0)

# ============================================================
# ARM REST POSE - "arms down" instead of T-pose
# ============================================================
ARM_DOWN_Z = 75.0

LEFT_UPPER_ARM_REST = (0, 0, ARM_DOWN_Z)
RIGHT_UPPER_ARM_REST = (0, 0, -ARM_DOWN_Z)

LEFT_LOWER_ARM_REST = (0, 0, 8.0)
RIGHT_LOWER_ARM_REST = (0, 0, -8.0)

BONE_NAMES = {
    "head": "Head",
    "neck": "Neck",
    "spine": "Spine",
    "chest": "Chest",
    "hips": "Hips",
    "left_upper_arm": "LeftUpperArm",
    "right_upper_arm": "RightUpperArm",
    "left_lower_arm": "LeftLowerArm",
    "right_lower_arm": "RightLowerArm",
    "left_hand": "LeftHand",
    "right_hand": "RightHand",
    "left_eye": "LeftEye",
    "right_eye": "RightEye",
}

# ============================================================
# BLENDSHAPE NAMES
# ============================================================

MOUTH_BLEND_NAMES = {
    "a": "A",
    "i": "I",
    "u": "U",
    "e": "E",
    "o": "O",
}

EXPRESSION_BLEND_NAMES = {
    "neutral": None,
    "happy": "Joy",
    "sad": "Sorrow",
    "angry": "Angry",
    "excited": "Joy",
    "gentle": "Joy",
}

# Blink blendshape - confirm this matches your model's VRM blendshape name.
# Common alternatives: "Blink", "BLINK", "Blink_L"/"Blink_R" (separate eyes)
BLINK_BLEND_NAME = "Blink"


def vowel_to_mouth_open(vowel: str) -> float:
    v = (vowel or "").lower()
    if v == "a":
        return 1.0
    if v == "o":
        return 0.9
    if v == "e":
        return 0.8
    if v == "u":
        return 0.5
    if v == "i":
        return 0.4
    return 0.0


def vowel_to_blend_name(vowel: str) -> Optional[str]:
    return MOUTH_BLEND_NAMES.get((vowel or "").lower())


def build_mouth_timeline_from_voicevox(audio_query: Dict, fps: float = 60.0) -> List[Dict]:
    frame_dt = 1.0 / fps
    segments = []
    t = 0.0

    accent_phrases = audio_query.get("accent_phrases", [])
    for phrase in accent_phrases:
        for mora in phrase.get("moras", []):
            c_len = float(mora.get("consonant_length") or 0.0)
            if c_len > 0.0:
                segments.append((t, t + c_len, 0.2, None))
                t += c_len

            v_len = float(mora.get("vowel_length") or 0.0)
            if v_len > 0.0:
                vowel = mora.get("vowel") or ""
                segments.append((t, t + v_len, vowel_to_mouth_open(vowel), vowel))
                t += v_len

        pause_mora = phrase.get("pause_mora")
        if pause_mora:
            pause_len = float(pause_mora.get("vowel_length") or 0.0)
            if pause_len > 0.0:
                segments.append((t, t + pause_len, 0.0, None))
                t += pause_len

    if not segments:
        return []

    total_time = segments[-1][1]
    num_frames = int(total_time * fps) + 2

    frames = []
    seg_idx = 0
    for frame_idx in range(num_frames):
        current_time = frame_idx * frame_dt
        while seg_idx + 1 < len(segments) and segments[seg_idx][1] <= current_time:
            seg_idx += 1
        _, _, open_val, vowel = segments[seg_idx]
        frames.append({"open": float(open_val), "vowel": vowel})

    return frames


class VMCClient:
    """
    Mimics the parts of VTubeStudioClient your bot script calls:
    is_connected, connect(), close(). Internally sends VMC OSC.
    """

    def __init__(
        self,
        ready_event: Optional[asyncio.Event] = None,
        shutdown_event: Optional[asyncio.Event] = None,
        ip: str = "127.0.0.1",
        port: int = 39539,
        auth_token: str = "",
    ):
        self.ip = ip
        self.port = port
        self.client = SimpleUDPClient(ip, port)
        self.ready_event = ready_event
        self.shutdown_event = shutdown_event
        self.is_connected = False
        self.current_expression = "neutral"
        self.is_talking = False

    async def connect(self):
        self.is_connected = True
        if self.ready_event:
            self.ready_event.set()
        print(f"VMC client ready -> sending to {self.ip}:{self.port}")

        while self.shutdown_event is None or not self.shutdown_event.is_set():
            await asyncio.sleep(1)

    async def close(self):
        self.is_connected = False

    # ---------------- low level senders ----------------

    def send_bone(self, bone, pos, pitch: float = 0.0, yaw: float = 0.0, roll: float = 0.0):
        rot = R.from_euler('xyz', [pitch, yaw, roll], degrees=True)
        qx, qy, qz, qw = rot.as_quat(canonical=True)
        self.client.send_message(
            "/VMC/Ext/Bone/Pos",
            [bone, pos[0], pos[1], pos[2], qx, qy, qz, qw]
        )

    def send_blend(self, name, value):
        self.client.send_message("/VMC/Ext/Blend/Val", [name, value])

    def apply(self):
        self.client.send_message("/VMC/Ext/Blend/Apply", [])

    def reset_mouth(self):
        for name in MOUTH_BLEND_NAMES.values():
            self.send_blend(name, 0.0)
        self.apply()

    def set_expression(self, expression: str):
        self.current_expression = expression
        for blend_name in EXPRESSION_BLEND_NAMES.values():
            if blend_name:
                self.send_blend(blend_name, 0.0)
        target = EXPRESSION_BLEND_NAMES.get((expression or "neutral").lower())
        if target:
            self.send_blend(target, 1.0)
        self.apply()


# ============================================================
# LIPSYNC
# ============================================================

async def play_audio_and_lipsync_vmc(
    vmc: VMCClient,
    audio_bytes: bytes,
    audio_query: Optional[Dict] = None,
    fps: float = 60.0,
    play_audio_fn=None,
):
    if not audio_bytes or not vmc or not vmc.is_connected:
        print("[VMC lipsync] No audio or VMC not connected.")
        return

    try:
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes), format="wav")
    except Exception as e:
        print(f"[VMC lipsync] Failed to decode audio: {e}")
        return

    frame_dt = 1.0 / fps

    if play_audio_fn:
        audio_task = asyncio.create_task(asyncio.to_thread(play_audio_fn, audio_bytes))
    else:
        audio_task = None

    vmc.is_talking = True

    mouth_timeline = None
    if audio_query is not None:
        try:
            mouth_timeline = build_mouth_timeline_from_voicevox(audio_query, fps=fps)
        except Exception as e:
            print(f"[VMC lipsync] phoneme build error: {e}")
            mouth_timeline = None

    if mouth_timeline:
        current_open = 0.0
        current_vowel_blend = None
        SMOOTH = 0.6

        for frame in mouth_timeline:
            if not vmc.is_connected:
                break

            target_open = frame["open"]
            vowel_blend = vowel_to_blend_name(frame["vowel"]) if frame["vowel"] else current_vowel_blend

            current_open += (target_open - current_open) * SMOOTH

            if vowel_blend != current_vowel_blend and current_vowel_blend:
                vmc.send_blend(current_vowel_blend, 0.0)

            if vowel_blend:
                vmc.send_blend(vowel_blend, current_open)
                current_vowel_blend = vowel_blend

            vmc.apply()
            await asyncio.sleep(frame_dt)

        if audio_task:
            await audio_task

        vmc.reset_mouth()
        vmc.is_talking = False
        return

    # RMS fallback
    samples = np.array(audio.get_array_of_samples(), dtype=np.float32)
    if audio.channels == 2:
        samples = samples.reshape((-1, 2)).mean(axis=1)

    if samples.size == 0:
        if audio_task:
            await audio_task
        vmc.is_talking = False
        return

    max_int = float(2 ** (8 * audio.sample_width - 1)) or 32768.0
    data = samples / max_int

    frame_size = max(1, int(audio.frame_rate * frame_dt))
    n = int(data.shape[0])
    num_frames = max(1, int(np.ceil(n / frame_size)))

    rms_vals = []
    for i in range(num_frames):
        start = i * frame_size
        end = min(start + frame_size, n)
        frame = data[start:end]
        rms_vals.append(float(np.sqrt(np.mean(frame * frame))) if frame.size else 0.0)

    max_rms = max(rms_vals) or 1e-6
    current_open = 0.0
    SMOOTH = 0.35
    GAIN = 1.2

    for rms in rms_vals:
        if not vmc.is_connected:
            break
        target = min((rms / max_rms) * GAIN, 1.0)
        current_open += (target - current_open) * SMOOTH
        vmc.send_blend("A", current_open)
        vmc.apply()
        await asyncio.sleep(frame_dt)

    if audio_task:
        await audio_task

    vmc.reset_mouth()
    vmc.is_talking = False


# ============================================================
# FULL BODY IDLE ANIMATION
# Arms down + breathing + weight shift + head sway + gestures
# + blinking + eye saccades
# ============================================================

async def vmc_idle_loop(vmc: VMCClient, shutdown_evt: asyncio.Event):
    """
    Runs continuously. Sends natural idle motion for head, spine,
    hips, both arms, eyes, and blinking every frame so the model
    never reverts to T-pose and doesn't look like a static mannequin.
    """

    vmc.is_talking = False

    gesture_until = 0.0
    gesture_type = None
    next_gesture_time = time.time() + random.uniform(8, 16)

    # ---- blink state ----
    next_blink_time = time.time() + random.uniform(2.0, 6.0)
    blink_until = 0.0
    blink_start = 0.0
    BLINK_DURATION = 0.15  # seconds for a full close-open cycle

    # ---- eye saccade state ----
    eye_target_yaw = 0.0
    eye_target_pitch = 0.0
    eye_current_yaw = 0.0
    eye_current_pitch = 0.0
    next_saccade_time = time.time() + random.uniform(1.5, 4.0)

    while not getattr(shutdown_evt, "is_set", lambda: False)():
        if not vmc.is_connected:
            await asyncio.sleep(0.5)
            continue

        import ws_animation_server as _ws
        if _ws.is_vmc_receiving():
            await asyncio.sleep(0.5)
            continue

        t = time.time()

        # ---------------- HEAD ----------------
        head_pitch = math.sin(t * 0.8) * 2
        head_yaw = math.sin(t * 0.3) * 3
        head_roll = math.sin(t * 0.5) * 2

        # ---------------- BREATHING ----------------
        breathe = math.sin(t * 1.2) * 1.2
        spine_pitch = math.sin(t * 0.5) * 1.5 + breathe * 0.3
        spine_roll = math.sin(t * 0.3) * 1.5

        # ---------------- WEIGHT SHIFT ----------------
        weight_shift = math.sin(t * 0.25) * 4.0

        # ---------------- ARMS ----------------
        arm_sway = math.sin(t * 0.6) * 2.0

        left_upper = (
            LEFT_UPPER_ARM_REST[0],
            LEFT_UPPER_ARM_REST[1] + arm_sway * 0.3,
            LEFT_UPPER_ARM_REST[2] + arm_sway,
        )
        right_upper = (
            RIGHT_UPPER_ARM_REST[0],
            RIGHT_UPPER_ARM_REST[1] - arm_sway * 0.3,
            RIGHT_UPPER_ARM_REST[2] - arm_sway,
        )
        left_lower = LEFT_LOWER_ARM_REST
        right_lower = RIGHT_LOWER_ARM_REST

        # ---------------- GESTURES ----------------
        if t >= next_gesture_time and gesture_type is None:
            gesture_type = random.choice(["head_tilt", "shoulder_shrug", "look_away"])
            gesture_until = t + random.uniform(1.5, 3.0)

        if gesture_type and t < gesture_until:
            progress = (t - (gesture_until - 3.0)) / 3.0
            wave = math.sin(max(0.0, min(1.0, progress)) * math.pi)

            if gesture_type == "head_tilt":
                head_roll += 12 * wave
                head_yaw += 6 * wave
            elif gesture_type == "shoulder_shrug":
                left_upper = (left_upper[0], left_upper[1], left_upper[2] - 8 * wave)
                right_upper = (right_upper[0], right_upper[1], right_upper[2] + 8 * wave)
            elif gesture_type == "look_away":
                head_yaw += 18 * wave

        elif gesture_type and t >= gesture_until:
            gesture_type = None
            next_gesture_time = t + random.uniform(8, 16)

        # ---------------- EYE SACCADES ----------------
        if t >= next_saccade_time:
            eye_target_yaw = random.uniform(-12, 12)
            eye_target_pitch = random.uniform(-6, 6)
            next_saccade_time = t + random.uniform(1.5, 4.0)

        # smooth toward target
        eye_current_yaw += (eye_target_yaw - eye_current_yaw) * 0.15
        eye_current_pitch += (eye_target_pitch - eye_current_pitch) * 0.15

        vmc.send_bone(BONE_NAMES["left_eye"], ZERO_POS, eye_current_pitch, eye_current_yaw, 0)
        vmc.send_bone(BONE_NAMES["right_eye"], ZERO_POS, eye_current_pitch, eye_current_yaw, 0)

        # ---------------- BLINK ----------------
        if t >= next_blink_time and blink_until == 0.0:
            blink_start = t
            blink_until = t + BLINK_DURATION
            next_blink_time = t + random.uniform(2.5, 6.0)

        if blink_until > 0.0:
            if t < blink_until:
                progress = (t - blink_start) / BLINK_DURATION
                # close then open: triangle wave 0 -> 1 -> 0
                blink_val = 1.0 - abs(progress - 0.5) 