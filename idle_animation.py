import time
import math
import random
import threading
import sys

from pythonosc.udp_client import SimpleUDPClient
from scipy.spatial.transform import Rotation as R

client = SimpleUDPClient("127.0.0.1", 40000)

HEAD_POS  = (-1.359684178809672e-10, 0.05747377872467041, 0.007213786244392395)
SPINE_POS = (0.0, 0.04311231772402344, 0.010174106800651664)
EYE_POS   = (-0.0137, 0.05195, 0.0818)
ZERO_POS  = (0.0, 0.0, 0.0)


_prev_quats = {}  # bone name -> last sent (qx, qy, qz, qw)


def send_bone(bone, pos, pitch=0.0, yaw=0.0, roll=0.0):
    rot = R.from_euler('xyz', [pitch, yaw, roll], degrees=True)
    q = rot.as_quat(canonical=False)  # NOT canonical -- we enforce continuity ourselves below

    prev = _prev_quats.get(bone)
    if prev is not None:
        # q and -q represent the identical rotation. Forcing a fixed
        # convention (e.g. w>=0) can flip the sign between consecutive
        # frames even when the actual rotation barely changed, which
        # reads on the receiving side as a sudden snap/twist. Instead,
        # pick whichever sign is closer to the previous frame's
        # quaternion so the sequence stays continuous.
        dot = prev[0]*q[0] + prev[1]*q[1] + prev[2]*q[2] + prev[3]*q[3]
        if dot < 0.0:
            q = (-q[0], -q[1], -q[2], -q[3])

    _prev_quats[bone] = tuple(q)
    qx, qy, qz, qw = q
    client.send_message("/VMC/Ext/Bone/Pos",
                        [bone, pos[0], pos[1], pos[2], qx, qy, qz, qw])


def send_blend(name, value):
    client.send_message("/VMC/Ext/Blend/Val", [name, value])


def apply():
    client.send_message("/VMC/Ext/Blend/Apply", [])


# ============================================================
# SAFE ANGLE CLAMP
# ============================================================

def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def clamp_pose(pose):
    return {
        "head_pitch":  clamp(pose.get("head_pitch",  0), -28, 35),
        "head_yaw":    clamp(pose.get("head_yaw",    0), -45, 45),
        "head_roll":   clamp(pose.get("head_roll",   0), -25, 25),
        "spine_pitch": clamp(pose.get("spine_pitch", 0), -16, 18),
        "spine_roll":  clamp(pose.get("spine_roll",  0), -12, 12),
        "blend": pose.get("blend", {})
    }


def lerp(current, target, alpha):
    return current + (target - current) * alpha


def blend_poses(a, b, t):
    """Linear interpolation between two clamped pose dicts. t in [0,1]."""
    out = {
        "head_pitch":  lerp(a["head_pitch"],  b["head_pitch"],  t),
        "head_yaw":    lerp(a["head_yaw"],    b["head_yaw"],    t),
        "head_roll":   lerp(a["head_roll"],   b["head_roll"],   t),
        "spine_pitch": lerp(a["spine_pitch"], b["spine_pitch"], t),
        "spine_roll":  lerp(a["spine_roll"],  b["spine_roll"],  t),
    }
    keys = set(a["blend"]) | set(b["blend"])
    out["blend"] = {k: lerp(a["blend"].get(k, 0.0), b["blend"].get(k, 0.0), t) for k in keys}
    return out


def smootherstep(x):
    x = clamp(x, 0.0, 1.0)
    return x * x * x * (x * (x * 6 - 15) + 10)


def _ease_out_back(x):
    c1, c3 = 1.70158, 2.70158
    x = clamp(x, 0.0, 1.0)
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


# ============================================================
# dt-BASED SMOOTHING
# ============================================================
POSE_HALF_LIFE  = 0.18
BLEND_HALF_LIFE = 0.14
TALK_HALF_LIFE  = 0.05
MAX_DEG_PER_SEC = 95.0  # raised again alongside the bigger amplitudes below

# How long a state-to-state crossfade takes. This is the main fix for
# "jerky/snapping" transitions: instead of only rate-limiting the final
# bone value, we blend the *pose functions themselves* over this window.
STATE_TRANSITION_TIME = 0.6

_last_frame_time = time.time()


def _decay(half_life: float, dt: float) -> float:
    if half_life <= 0:
        return 1.0
    return 1.0 - math.pow(0.5, dt / half_life)


def lerp_clamped(current, target, alpha, dt):
    delta = (target - current) * alpha
    max_delta = MAX_DEG_PER_SEC * dt
    if abs(delta) > max_delta:
        delta = math.copysign(max_delta, delta)
    return current + delta


def _gauss_clamped(sigma_range):
    v = random.gauss(0, sigma_range / 2.5)
    return clamp(v, -sigma_range, sigma_range)


# ============================================================
# ORGANIC MOTION
# Sum of a few sine waves at incommensurate frequencies/phases per
# seed, instead of one clean sin(t*f). This is what kills the
# "obvious metronome loop" look -- it never repeats on a short,
# noticeable cycle the way a single sine does.
# ============================================================
def organic(t, seed, freq=0.3, amp=1.0, layers=3):
    v = 0.0
    wsum = 0.0
    for i in range(layers):
        w = 1.0 / (i + 1)
        f = freq * (1.0 + i * 0.372 + seed * 0.041)
        p = seed * 7.31 + i * 2.63
        v += math.sin(t * f + p) * w
        wsum += w
    return (v / wsum) * amp


# ============================================================
# GENERIC DRIFT -- random target + exponential smoothing toward it
# (an Ornstein-Uhlenbeck-ish process). One implementation shared by
# arms, sway/weight-shift, and head drift instead of four
# copy-pasted versions.
# ============================================================
class Drift:
    def __init__(self, ranges, interval=(4.0, 9.0), half_life=0.6):
        self.ranges = dict(ranges)
        self.target = {k: 0.0 for k in ranges}
        self.current = {k: 0.0 for k in ranges}
        self.interval = interval
        self.half_life = half_life
        self.next_change = time.time() + random.uniform(*interval)

    def update(self, now, dt, overrides=None):
        if now >= self.next_change:
            for k, rng in self.ranges.items():
                self.target[k] = random.uniform(-rng, rng)
            if overrides:
                overrides(self.target)
            self.next_change = now + random.uniform(*self.interval)
        a = _decay(self.half_life, dt)
        for k in self.current:
            self.current[k] += (self.target[k] - self.current[k]) * a
        return self.current


# ============================================================
# ARM POSE -- natural resting stance
# A small permanent left/right asymmetry (ARM_L_BIAS / ARM_R_BIAS)
# stops the arms reading as a perfectly mirrored mannequin pose, and
# an extra wrist-pitch drift axis keeps hands from looking locked to
# a single rotation axis.
# ============================================================
SHOULDER_DROP = 3.0
ARM_INWARD    = 8.0
ARM_DROP      = 72.0
ELBOW_BEND    = 22.0

# Bumped up from the original (1.5/-1.0/etc.) -- perfectly mirrored
# arms is a big part of what reads as "mannequin standing," so this
# asymmetry needs to be visible, not subtle.
ARM_L_BIAS = {"upper_roll": 2.5, "lower_bend": 3.0, "hand_roll": -1.5}
ARM_R_BIAS = {"upper_roll": -1.5, "lower_bend": 1.5, "hand_roll": 2.0}

_arm_l_drift = Drift(
    {"upper_roll": 10.0, "lower_bend": 11.0, "hand_roll": 10.0, "hand_pitch": 7.0},
    interval=(3.0, 7.0), half_life=0.7)
_arm_r_drift = Drift(
    {"upper_roll": 10.0, "lower_bend": 11.0, "hand_roll": 10.0, "hand_pitch": 7.0},
    interval=(3.0, 7.0), half_life=0.7)

_arm_pose_start_time = time.time()
ARM_POSE_EASE_IN = 1.5  # seconds to ease into full arm drop on startup


def arm_ease_factor(now):
    elapsed = now - _arm_pose_start_time
    if elapsed >= ARM_POSE_EASE_IN:
        return 1.0
    return smootherstep(elapsed / ARM_POSE_EASE_IN)


def arm_bones(t, now, dt):
    l = _arm_l_drift.update(now, dt)
    r = _arm_r_drift.update(now, dt)
    ease = arm_ease_factor(now)
    return {
        "LeftShoulder":  (0, 0,  SHOULDER_DROP * ease),
        "RightShoulder": (0, 0, -SHOULDER_DROP * ease),
        "LeftUpperArm":  (0,  (ARM_INWARD + ARM_L_BIAS["upper_roll"] + l["upper_roll"]) * ease,  ARM_DROP * ease),
        "RightUpperArm": (0, -(ARM_INWARD + ARM_R_BIAS["upper_roll"] + r["upper_roll"]) * ease, -ARM_DROP * ease),
        "LeftLowerArm":  (l["hand_pitch"] * 0.3 * ease,
                           (ELBOW_BEND + ARM_L_BIAS["lower_bend"] + l["lower_bend"]) * ease, 0),
        "RightLowerArm": (r["hand_pitch"] * 0.3 * ease,
                           -(ELBOW_BEND + ARM_R_BIAS["lower_bend"] + r["lower_bend"]) * ease, 0),
        "LeftHand":  (l["hand_pitch"] * ease, 0,  (ARM_L_BIAS["hand_roll"] + l["hand_roll"]) * ease),
        "RightHand": (r["hand_pitch"] * ease, 0, -(ARM_R_BIAS["hand_roll"] + r["hand_roll"]) * ease),
    }


# ============================================================
# BODY SWAY + WEIGHT SHIFT + OCCASIONAL BODY TURN
# Standard "looks weighted, not floating" trick: a slow lateral hip
# offset paired with a small counter-lean in the spine, layered under
# the faster small sway. No leg bones are touched (assumed IK/none
# controlled here) -- the illusion is carried entirely by hips+spine.
#
# hip_x / spine_counter amplitudes bumped up from the original
# (0.022 / 5.0) -- the original was too subtle to read as a clear
# weight-on-one-hip stance, which is part of why she looked flat/
# mannequin-like standing dead-on to camera.
# ============================================================
_sway = Drift({"spine_roll": 4.5, "spine_pitch": 2.8, "hip_roll": 3.5},
              interval=(4.0, 9.0), half_life=0.8)
_weight_shift = Drift({"hip_x": 0.035, "spine_counter": 8.0},
                       interval=(6.0, 14.0), half_life=1.5)

# FACE-FRONT MODE: this used to be a "whole-body turn" that drifted up
# to 22 degrees off-axis every 18-40s, and it *started* already offset
# 15 degrees away from camera -- so she never actually had a "resting
# facing" at all, just a slow endless wander. That's the single
# biggest contributor to the "haphazard Live2D idle" look. Collapsed
# to a tiny 3-degree wobble centered on 0 (dead-on to camera) over a
# much longer interval, so there's still a hint of life but she always
# settles back toward facing front.
BODY_TURN_RANGE = 3.0  # was 22.0
_body_turn = Drift({"yaw": BODY_TURN_RANGE}, interval=(30.0, 60.0), half_life=2.5)
_body_turn.current["yaw"] = 0.0
_body_turn.target["yaw"] = 0.0


def update_body_sway(now, dt):
    s = _sway.update(now, dt)
    w = _weight_shift.update(now, dt)
    spine_pitch = s["spine_pitch"]
    spine_roll  = s["spine_roll"] + w["spine_counter"] * 0.4
    hip_roll    = s["hip_roll"]
    hip_x       = w["hip_x"]
    return spine_pitch, spine_roll, hip_roll, hip_x


def update_body_turn(now, dt):
    return _body_turn.update(now, dt)["yaw"]


# ============================================================
# GESTURES -- one-shot, additive, stack on top of whatever
# state/idle pose is currently playing. Amplitude and duration get a
# small random jitter per trigger so repeats don't feel identical.
#
# NOTE: the envelope used to use an "ease-out-back" curve, which
# deliberately overshoots past its target before settling -- a nice
# cartoony bounce, but combined with force_state() firing a gesture
# on every single emotion change (see below), it was a big source of
# the "jerky/snapping" feel. Swapped to smootherstep (no overshoot).
# ============================================================
GESTURES = {
    "hand_raise":    {"RightUpperArm": (0, 0, -50), "RightLowerArm": (0, -30, 0)},
    "point":         {"RightUpperArm": (0, -15, -30), "RightLowerArm": (0, -65, 0)},
    "chest_touch":   {"RightUpperArm": (0, 28, -65), "RightLowerArm": (0, -75, 0)},
    "shrug":         {"LeftUpperArm": (0, 15, 30), "RightUpperArm": (0, -15, -30),
                       "LeftShoulder": (0, 0, -10), "RightShoulder": (0, 0, 10)},
    "head_tilt_big": {"Head": (0, 0, 30)},
    "recoil":        {"Spine": (-12, 0, 0), "Head": (-9, 0, 0)},
    "lean_in":       {"Spine": (9, 0, 0), "Head": (6, 0, 0)},
    "hip_pop":       {"Hips": (0, 0, 18)},
    "nod_big":       {"Head": (20, 0, 0)},
    "shake_big":     {"Head": (0, 26, 0)},
    # Held noticeably longer than the snap-gestures above (see the
    # "_duration" override below) so it reads as a deliberate bow
    # rather than a quick twitch -- main.py fires this ~0.6s before the
    # greeting line starts playing, so it needs to still be visibly
    # mid-bow at that point.
    "bow":           {"Spine": (34, 0, 0), "Head": (20, 0, 0)},
}
# Bumped from 0.55 -- longer attack/hold/release reads as a softer,
# more natural gesture instead of a snap, especially now that fidgets
# fire less often but should still feel smooth when they do.
GESTURE_DURATION = 0.9
# Per-gesture duration overrides, in seconds, for gestures that
# shouldn't use the default quick snap timing.
GESTURE_DURATION_OVERRIDES = {
    "bow": 1.4,
}

_active_gestures = []  # dicts: {"name", "start", "amp", "duration"}


def trigger_gesture(name, amp=1.0):
    if name not in GESTURES:
        return
    boost = (1.0 + talk_amplitude * 0.5) if is_talking else 1.0
    boosted = amp * boost * random.uniform(0.9, 1.08)
    base_duration = GESTURE_DURATION_OVERRIDES.get(name, GESTURE_DURATION)
    duration = base_duration * random.uniform(0.92, 1.1)
    _active_gestures.append({"name": name, "start": time.time(), "amp": boosted, "duration": duration})


def gesture_envelope(elapsed, duration):
    """Attack / hold / release envelope. Uses smootherstep (no
    overshoot) instead of the old ease-out-back -- overshoot reads as
    an elastic bounce, which is fine used sparingly on purpose but
    looked like a snap/pop when gestures fired automatically."""
    if elapsed >= duration:
        return 0.0
    x = elapsed / duration
    if x < 0.25:
        return smootherstep(x / 0.25)
    elif x < 0.65:
        return 1.0
    else:
        return 1.0 - smootherstep((x - 0.65) / 0.35)


def compute_gesture_offsets(now):
    offsets = {}
    still_active = []
    for g in _active_gestures:
        dur = g["duration"]
        elapsed = now - g["start"]
        if elapsed >= dur:
            continue
        env = gesture_envelope(elapsed, dur) * g["amp"]
        for bone, (p, y, r) in GESTURES[g["name"]].items():
            cur = offsets.get(bone, (0.0, 0.0, 0.0))
            offsets[bone] = (cur[0] + p * env, cur[1] + y * env, cur[2] + r * env)
        still_active.append(g)
    _active_gestures[:] = still_active
    return offsets


# ============================================================
# GESTURE OFFSET SMOOTHING
# compute_gesture_offsets() above already has its own attack/hold/
# release envelope, but that raw value used to be added straight onto
# the already-smoothed bone value with zero additional frame-to-frame
# smoothing. Any sudden envelope change -- two gestures overlapping,
# a gesture starting the instant another ends -- popped instantly.
# Running the raw per-bone offsets through a short low-pass filter
# here removes that pop without meaningfully delaying the gesture.
# Half-life bumped from 0.09 -- slightly longer still reads as
# essentially instant but blends overlapping gestures more gently.
# ============================================================
_smooth_gesture_offsets = {}
_prev_hip_x = 0.0
_smooth_hip_y = 0.0
HIP_SETTLE_STRENGTH = 3.5   # how much lateral movement speed converts to dip
HIP_SETTLE_MAX = 0.006      # meters -- keep this SMALL, it's a subtle cue not a bounce
HIP_SETTLE_HALF_LIFE = 0.12
GESTURE_SMOOTH_HALF_LIFE = 0.12


def smooth_gesture_offsets(dt, raw_offsets):
    global _smooth_gesture_offsets
    a = _decay(GESTURE_SMOOTH_HALF_LIFE, dt)
    all_bones = set(_smooth_gesture_offsets) | set(raw_offsets)
    out = {}
    for bone in all_bones:
        cur = _smooth_gesture_offsets.get(bone, (0.0, 0.0, 0.0))
        tgt = raw_offsets.get(bone, (0.0, 0.0, 0.0))
        out[bone] = tuple(cur[i] + (tgt[i] - cur[i]) * a for i in range(3))
    _smooth_gesture_offsets = out
    return out


STATE_GESTURE_MAP = {
    "surprised":   "recoil",
    "happy":       "hand_raise",
    "excited":     "hand_raise",
    "angry":       "point",
    "shy":         "chest_touch",
    "smug":        "hip_pop",
    "curious":     "head_tilt_big",
    "questioning": "head_tilt_big",
    "sad":         None,
    "alert":       "recoil",
}

# ============================================================
# ENTRANCE SEQUENCE -- peek-in -> hold -> retreat -> walk-in
# main.py's wait_for_entrance_and_bow() assumes this takes ~3.2s
# total and fires BOW at 3.5s, so the phase timings below are tuned
# to land the walk-in's arrival right around that mark.
#
# FIX: head_yaw / spine_pitch used to jump straight to each phase's
# flat target value the instant that phase began (only hip_x was
# ever eased with smootherstep). That produced a hard snap at the
# start of the entrance and again at every phase boundary. Every
# phase below now eases head_yaw/spine_pitch with the same
# smootherstep curve used for hip_x, so nothing jumps.
#
# Note: there are no leg bones sent anywhere else in this script (the
# VMC receiver never gets LeftUpperLeg/etc. rotations), so nothing
# using this VMC feed has ever had a walk cycle -- only the hip
# position was sliding. The leg swing added in _entrance_leg_swing()
# below only plays during the walk-in phase, matching the same
# "speed drives a walk-cycle blend" shape used for the browser-side
# viewer's locomotion.
# ============================================================
ENTRANCE_PEEK_END    = 0.5   # ease from offscreen to the peek position
ENTRANCE_HOLD_END    = 1.1   # hold at the peek position
ENTRANCE_RETREAT_END = 1.6   # duck back offscreen
ENTRANCE_WALKIN_END  = 3.2   # walk from offscreen to the final rest spot

ENTRANCE_OFFSCREEN_X = -0.55
ENTRANCE_PEEK_X      = -0.18

_entrance_active = False
_entrance_start = 0.0


def start_entrance():
    global _entrance_active, _entrance_start
    _entrance_active = True
    _entrance_start = time.time()
    print("[entrance] sequence started")


def _entrance_leg_swing(t, speed):
    """Leg bones aren't driven anywhere else in this script -- this is
    only active during the entrance walk-in phase, proportional to
    how fast she's currently walking in (speed 0..1)."""
    if speed < 0.02:
        return {}
    stride_freq = 1.8
    phase = t * stride_freq * 2 * math.pi
    swing = 22.0 * speed
    return {
        "LeftUpperLeg":  (math.sin(phase) * swing, 0, 0),
        "RightUpperLeg": (math.sin(phase + math.pi) * swing, 0, 0),
        "LeftLowerLeg":  (max(0.0, -math.sin(phase)) * swing * 0.7, 0, 0),
        "RightLowerLeg": (max(0.0, -math.sin(phase + math.pi)) * swing * 0.7, 0, 0),
    }


def compute_entrance(now):
    """Returns (hip_x, head_yaw_offset, spine_pitch_offset, blend_dict,
    leg_offsets, still_active). hip_x replaces the normal weight-shift
    hip_x entirely while the entrance is active, since the two would
    otherwise fight over the same bone.

    Every phase boundary below eases head_yaw/spine_pitch/blend from
    the previous phase's value to the new phase's target using the
    same smootherstep curve as hip_x, so nothing snaps."""
    global _entrance_active
    if not _entrance_active:
        return 0.0, 0.0, 0.0, {}, {}, False

    elapsed = now - _entrance_start

    if elapsed < ENTRANCE_PEEK_END:
        bt = smootherstep(elapsed / ENTRANCE_PEEK_END)
        x = lerp(ENTRANCE_OFFSCREEN_X, ENTRANCE_PEEK_X, bt)
        head_yaw = lerp(0.0, 16.0, bt)
        spine_pitch = lerp(0.0, -3.0, bt)
        return x, head_yaw, spine_pitch, {"Blush": 0.2 * bt}, {}, True

    if elapsed < ENTRANCE_HOLD_END:
        return ENTRANCE_PEEK_X, 16.0 + organic(elapsed, 40, 0.6, 4.0), -3.0, {"Blush": 0.2, "Smile": 0.15}, {}, True

    if elapsed < ENTRANCE_RETREAT_END:
        span = ENTRANCE_RETREAT_END - ENTRANCE_HOLD_END
        bt = smootherstep((elapsed - ENTRANCE_HOLD_END) / span)
        x = lerp(ENTRANCE_PEEK_X, ENTRANCE_OFFSCREEN_X, bt)
        head_yaw = lerp(16.0, 10.0, bt)
        spine_pitch = lerp(-3.0, -1.5, bt)
        blush = lerp(0.2, 0.15, bt)
        return x, head_yaw, spine_pitch, {"Blush": blush}, {}, True

    if elapsed < ENTRANCE_WALKIN_END:
        span = ENTRANCE_WALKIN_END - ENTRANCE_RETREAT_END
        frac = (elapsed - ENTRANCE_RETREAT_END) / span
        bt = smootherstep(frac)
        x = lerp(ENTRANCE_OFFSCREEN_X, 0.0, bt)
        head_yaw = lerp(10.0, 0.0, bt)
        spine_pitch = lerp(-1.5, 0.0, bt)
        # speed ramps up then eases out on arrival, rather than a flat
        # walk speed the whole way -- matches how the walk-cycle blend
        # is handled on the browser side.
        speed = math.sin(clamp(frac, 0.0, 1.0) * math.pi)
        legs = _entrance_leg_swing(now, speed)
        return x, head_yaw, spine_pitch, {}, legs, True

    _entrance_active = False
    print("[entrance] sequence complete")
    return 0.0, 0.0, 0.0, {}, {}, False

# Trimmed from the original 5-gesture list -- shake_big/nod_big were
# large, sharp head snaps (20-26 degrees) firing every 4-9 seconds,
# which is what read as "tweaking/jerking" during idle. Those two are
# still available for real reactive gestures (via GESTURE:/REACT:),
# just no longer used as passive idle filler.
FIDGET_GESTURES = ["hip_pop", "head_tilt_big", "shrug"]
_next_fidget = time.time() + random.uniform(10.0, 20.0)


def maybe_fidget(now, state):
    global _next_fidget
    if now < _next_fidget:
        return
    if state in ("neutral", "listening", "bored", "daydreaming", "thinking"):
        # Lower amplitude (was 0.9) alongside the longer interval below,
        # so idle fidgeting reads as a small natural shift rather than
        # a full-strength gesture popping off every few seconds.
        trigger_gesture(random.choice(FIDGET_GESTURES), amp=0.6)
    _next_fidget = now + random.uniform(10.0, 25.0)


# ============================================================
# AUTOMATIC BLINKING
# ============================================================
_next_blink  = time.time() + random.uniform(2.0, 5.0)
_blink_start = None
_blink_duration_current = 0.15  # duration of the blink currently in progress

# duration, interval_min, interval_max per state. A "sleepy" blink is
# slower/heavier and less frequent; "alert"/"nervous" blink faster and
# more often. States not listed fall back to _BLINK_DEFAULT. This ties
# blinking into the same emotional read as everything else, instead of
# it being the one thing that stays identical no matter what state
# she's in.
_BLINK_PROFILES = {
    "sleepy":    (0.28, 1.5, 4.0),
    "lazy":      (0.22, 2.5, 5.5),
    "bored":     (0.20, 2.5, 5.5),
    "sad":       (0.20, 2.5, 6.0),
    "nervous":   (0.11, 1.2, 2.8),
    "alert":     (0.10, 1.0, 2.5),
    "surprised": (0.10, 1.5, 4.0),
    "excited":   (0.12, 1.5, 3.5),
}
_BLINK_DEFAULT = (0.15, 2.0, 6.0)

# Chance of a quick double-blink (real people do this fairly often,
# a single perfectly uniform blink cadence reads as slightly robotic).
_DOUBLE_BLINK_CHANCE = 0.12
_DOUBLE_BLINK_GAP = (0.10, 0.18)


def blink_value(now, state="neutral"):
    global _next_blink, _blink_start, _blink_duration_current

    duration, interval_min, interval_max = _BLINK_PROFILES.get(state, _BLINK_DEFAULT)

    if _blink_start is None:
        if now >= _next_blink:
            _blink_start = now
            _blink_duration_current = duration
        else:
            return 0.0

    elapsed = now - _blink_start
    if elapsed >= _blink_duration_current:
        _blink_start = None
        if random.random() < _DOUBLE_BLINK_CHANCE:
            _next_blink = now + random.uniform(*_DOUBLE_BLINK_GAP)
        else:
            _next_blink = now + random.uniform(interval_min, interval_max)
            if random.random() < 0.1:
                _next_blink = now + random.uniform(0.2, 0.4)
        return 0.0

    return math.sin((elapsed / _blink_duration_current) * math.pi)

# ============================================================
# EYE SACCADES -- state-aware, resets on state change
# ============================================================
_eye_target_yaw    = 0.0
_eye_target_pitch  = 0.0
_eye_current_yaw   = 0.0
_eye_current_pitch = 0.0
_next_eye_move     = time.time() + random.uniform(3.0, 6.0)
_eye_current_state = "neutral"

_EYE_PROFILES = {
    "neutral":        (2.5, 1.5, 2.5, 5.0),
    "listening":      (2.0, 1.0, 2.5, 5.0),
    "thinking":       (3.0, 2.0, 3.0, 6.0),
    "happy":          (3.0, 1.5, 3.0, 6.0),
    "shy":            (5.0, 3.0, 2.0, 4.0),
    "sad":            (3.0, 2.0, 4.0, 8.0),
    "sleepy":         (1.5, 1.0, 6.0, 12.0),
    "looking_around": (10.0, 4.0, 1.5, 3.5),
    "curious":        (8.0, 3.0, 2.0, 4.0),
    "alert":          (10.0, 4.0, 1.0, 3.0),
    "nervous":        (7.0, 3.0, 1.0, 2.5),
    "bored":          (8.0, 3.0, 3.0, 6.0),
    "daydreaming":    (5.0, 3.0, 4.0, 8.0),
    "surprised":      (3.0, 2.0, 3.0, 6.0),
}
_EYE_DEFAULT = (2.5, 1.5, 2.5, 5.0)


def update_eyes(now, state="neutral"):
    global _eye_target_yaw, _eye_target_pitch
    global _eye_current_yaw, _eye_current_pitch, _next_eye_move
    global _eye_current_state

    yaw_range, pitch_range, interval_min, interval_max = _EYE_PROFILES.get(state, _EYE_DEFAULT)

    if state != _eye_current_state:
        _eye_current_state = state
        _next_eye_move = now

    if now >= _next_eye_move:
        if state in ("neutral", "listening", "happy", "smile") and random.random() < 0.5:
            _eye_target_yaw   = random.uniform(-0.5, 0.5)
            _eye_target_pitch = random.uniform(-0.3, 0.3)
        else:
            _eye_target_yaw   = random.uniform(-yaw_range, yaw_range)
            _eye_target_pitch = random.uniform(-pitch_range, pitch_range)
        _next_eye_move = now + random.uniform(interval_min, interval_max)

    a = _decay(0.1, 1 / 60)
    _eye_current_yaw   += (_eye_target_yaw   - _eye_current_yaw)   * a
    _eye_current_pitch += (_eye_target_pitch - _eye_current_pitch) * a

    return _eye_current_pitch, _eye_current_yaw


# ============================================================
# TALKING CONTROL
# ============================================================
is_talking     = False
talk_amplitude = 1.0

MOUTH_VISEMES  = ["A", "I", "U", "E", "O"]
VISEME_WEIGHTS = [0.40, 0.15, 0.15, 0.20, 0.10]

_current_viseme     = "A"
_next_viseme_change = 0.0
_smoothed_mouth_amp = 0.0


def start_talking():
    global is_talking
    is_talking = True


def stop_talking():
    global is_talking
    is_talking = False


def set_talk_amplitude(value):
    global talk_amplitude
    talk_amplitude = max(0.0, min(1.0, value))


def talking_blend(t):
    global _current_viseme, _next_viseme_change, _smoothed_mouth_amp
    if t >= _next_viseme_change:
        _current_viseme     = random.choices(MOUTH_VISEMES, weights=VISEME_WEIGHTS, k=1)[0]
        _next_viseme_change = t + random.uniform(0.08, 0.16)
    target_amp = talk_amplitude
    _smoothed_mouth_amp += (target_amp - _smoothed_mouth_amp) * 0.35
    open_amount = min(0.75, _smoothed_mouth_amp * 0.7)
    blend = {v: 0.0 for v in MOUTH_VISEMES}
    blend[_current_viseme] = open_amount
    head_bob = math.sin(t * 3.5) * 0.5 * talk_amplitude
    head_nod = math.sin(t * 0.8) * 0.8 * talk_amplitude
    return {"blend": blend, "head_pitch_offset": head_nod, "head_roll_offset": head_bob}


# ============================================================
# FORCED STATE
# ============================================================
forced_state       = None
forced_state_until = 0
_last_force_state_time = 0.0
FORCE_STATE_MIN_INTERVAL = 1.4  # seconds -- tune this; see note below

def force_state(name, duration=12.0):
    global forced_state, forced_state_until, _last_force_state_time
    if name not in STATE_FUNCS:
        return

    now = time.time()

    # Gate: ignore a new forced-state request that arrives too soon
    # after the previous one, UNLESS it's requesting the same state
    # again (e.g. just extending/refreshing duration, which is harmless
    # and shouldn't be suppressed). Two independent reaction sources
    # (a mood-based react fired right before she starts speaking, and
    # an inline [emotion:X] tag embedded in her actual reply text) can
    # each call this within a fraction of a second of each other for
    # the SAME utterance -- without this gate, that produces two
    # back-to-back pose crossfades to different poses, which reads as
    # a visible tilt-then-return tick instead of one clean reaction.
    if (name != forced_state
            and now - _last_force_state_time < FORCE_STATE_MIN_INTERVAL):
        print(f"[react] suppressed rapid re-trigger to '{name}' "
              f"({now - _last_force_state_time:.2f}s after last change)")
        return

    forced_state       = name
    forced_state_until = now + duration
    _last_force_state_time = now
    print(f"[react] forcing state: {name} for {duration}s")


# ============================================================
# EMOTION -> STATE MAPPING
# ============================================================
EMOTION_MAP = {
    "shocked": "surprised", "surprise": "surprised", "surprised": "surprised",
    "shocking": "surprised", "wow": "surprised", "whoa": "surprised",
    "omg": "surprised",
    "angry": "angry", "anger": "angry", "mad": "angry", "upset": "angry",
    "insult": "angry", "offended": "angry", "rude": "angry", "mean": "angry",
    "embarrassed": "shy", "embarrassing": "shy", "shy": "shy", "blush": "shy",
    "awkward": "shy", "cringe": "shy", "flirt": "shy",
    "happy": "happy", "happiness": "happy", "joy": "happy", "excited": "excited",
    "yay": "happy", "love": "happy", "great": "happy",
    "sad": "sad", "sadness": "sad", "cry": "sad", "sorry": "sad",
    "depressed": "sad",
    "curious": "curious", "curious?": "curious", "hmm": "thinking",
    "think": "thinking", "thinking": "thinking", "wonder": "thinking",
    "question": "questioning",
    "nervous": "nervous", "anxious": "nervous", "scared": "nervous",
    "fear": "nervous", "worried": "nervous",
    "smug": "smug", "proud": "smug", "confident": "smug",
    "lazy": "lazy", "bored": "bored", "boredom": "bored",
    "tired": "sleepy", "sleepy": "sleepy",
    "listening": "listening", "listen": "listening", "attentive": "listening",
    "neutral": "neutral", "calm": "neutral", "normal": "neutral", "reset": "neutral",
}


def resolve_emotion(keyword):
    return EMOTION_MAP.get(keyword.strip().lower(), "neutral")


# ============================================================
# STDIN LISTENER
# ============================================================
def _stdin_listener():
    for line in sys.stdin:
        cmd   = line.strip()
        upper = cmd.upper()
        if upper == "TALK_START":
            start_talking()
        elif upper == "TALK_STOP":
            stop_talking()
        elif upper.startswith("TALK_AMP:"):
            try:
                set_talk_amplitude(float(cmd.split(":", 1)[1]))
            except ValueError:
                pass
        elif upper.startswith("STATE:"):
            parts    = cmd.split(":")
            name     = parts[1].strip().lower() if len(parts) > 1 else ""
            duration = 12.0
            if len(parts) > 2:
                try: duration = float(parts[2])
                except ValueError: pass
            force_state(name, duration)
        elif upper.startswith("REACT:"):
            parts    = cmd.split(":")
            emotion  = parts[1].strip() if len(parts) > 1 else "neutral"
            duration = 12.0
            if len(parts) > 2:
                try: duration = float(parts[2])
                except ValueError: pass
            force_state(resolve_emotion(emotion), duration)
        elif upper.startswith("GESTURE:"):
            parts = cmd.split(":")
            name  = parts[1].strip().lower() if len(parts) > 1 else ""
            amp   = 1.0
            if len(parts) > 2:
                try: amp = float(parts[2])
                except ValueError: pass
            trigger_gesture(name, amp)
        elif upper == "ENTRANCE:START":
            start_entrance()
        elif upper == "BOW":
            trigger_gesture("bow", amp=1.0)


threading.Thread(target=_stdin_listener, daemon=True).start()


# ============================================================
# NATURAL HEAD DRIFT
# Gaussian-centered retargeting keeps her mostly facing forward
# toward camera with occasional bigger glances, rather than an
# even wander across the full range. Built on the shared Drift
# class via a retarget override.
#
# FACE-FRONT MODE: ranges pulled in and the retarget interval slowed
# down (was 1.6-4.0s -- a new target roughly every couple seconds is
# what read as restless glancing). Still Gaussian-centered on 0, so
# it's a gentle settle around camera-facing rather than an even
# wander across the full range.
# ============================================================
DRIFT_PITCH_RANGE = 4.0   # was 6.0
DRIFT_YAW_RANGE   = 4.5   # was 10.0
DRIFT_ROLL_RANGE  = 2.5   # was 4.5

_head_drift = Drift({"pitch": DRIFT_PITCH_RANGE, "yaw": DRIFT_YAW_RANGE, "roll": DRIFT_ROLL_RANGE},
                     interval=(2.5, 6.0), half_life=0.45)


def _head_drift_override(target):
    target["pitch"] = _gauss_clamped(DRIFT_PITCH_RANGE)
    target["yaw"]   = _gauss_clamped(DRIFT_YAW_RANGE)
    target["roll"]  = _gauss_clamped(DRIFT_ROLL_RANGE)


def update_head_drift(now, dt):
    d = _head_drift.update(now, dt, overrides=_head_drift_override)
    return d["pitch"], d["yaw"], d["roll"]


# ============================================================
# BREATHING -- depth wanders slowly so it doesn't look metronomic
# ============================================================
BREATH_RATE  = 0.25
BREATH_DEPTH = 2.2
_breath_depth_drift = Drift({"depth": 0.6}, interval=(20.0, 40.0), half_life=3.0)


def breathing(t, now, dt):
    depth = BREATH_DEPTH + _breath_depth_drift.update(now, dt)["depth"]
    return math.sin(t * BREATH_RATE * 2 * math.pi) * depth


# ============================================================
# STATES
# Every wobble below uses organic() instead of a bare sin(), so no
# state's idle motion reads as an obvious repeating loop. Seeds are
# distinct per axis so pitch/yaw/roll aren't phase-locked together.
# ============================================================

def neutral(t):
    # Deliberately flat: head_drift + breathing + weight_shift carry
    # neutral's idle life, so this state doesn't fight with them.
    return {"head_pitch": 0, "head_yaw": 0, "head_roll": 0,
            "spine_pitch": 0, "spine_roll": 0, "blend": {}}

def curious(t):
    return {"head_pitch": -5 + organic(t, 1, 0.18, 3.2),
            "head_yaw": organic(t, 2, 0.15, 13),
            "head_roll": 10 + organic(t, 3, 0.17, 5),
            "spine_pitch": -3, "spine_roll": 0,
            "blend": {"Surprised": 0.15, "Smile": 0.1}}

def questioning(t):
    return {"head_pitch": -3, "head_yaw": organic(t, 4, 0.16, 16),
            "head_roll": 16, "spine_pitch": 0, "spine_roll": 0,
            "blend": {"Surprised": 0.2}}

def thinking(t):
    return {"head_pitch": -13, "head_yaw": organic(t, 5, 0.09, 10),
            "head_roll": organic(t, 6, 0.11, 3.5),
            "spine_pitch": -4, "spine_roll": 0, "blend": {"Smile": 0.05}}

def listening(t):
    return {"head_pitch": -4, "head_yaw": organic(t, 7, 0.06, 6),
            "head_roll": 8 + organic(t, 8, 0.08, 3),
            "spine_pitch": 0, "spine_roll": 0, "blend": {"Smile": 0.1}}

def happy(t):
    return {"head_pitch": organic(t, 9, 0.28, 8),
            "head_yaw": organic(t, 10, 0.2, 9),
            "head_roll": organic(t, 11, 0.24, 9),
            "spine_pitch": 0, "spine_roll": 0,
            "blend": {"Smile": 0.8, "Joy": 0.7}}

def shy(t):
    return {"head_pitch": -18, "head_yaw": -8, "head_roll": 16,
            "spine_pitch": -5, "spine_roll": 0,
            "blend": {"Smile": 0.35, "Blush": 0.65}}

def looking_around(t):
    # Kept as-is for explicit REACT:/GESTURE: triggers -- just no
    # longer used as passive idle filler (see IDLE_VARIATIONS below).
    return {"head_pitch": 0, "head_yaw": organic(t, 12, 0.08, 16),
            "head_roll": 0, "spine_pitch": 0, "spine_roll": 0, "blend": {}}

def daydreaming(t):
    return {"head_pitch": 9, "head_yaw": organic(t, 13, 0.05, 5),  # was 13 -- smaller drift, stays front-facing
            "head_roll": 6, "spine_pitch": 0, "spine_roll": 0,
            "blend": {"Smile": 0.1}}

def excited(t):
    return {"head_pitch": organic(t, 14, 0.32, 10),
            "head_yaw": organic(t, 15, 0.24, 14),
            "head_roll": organic(t, 16, 0.36, 11),
            "spine_pitch": 0, "spine_roll": 0,
            "blend": {"Smile": 0.85, "Joy": 0.85}}

def angry(t):
    return {"head_pitch": -6 + organic(t, 17, 0.24, 3.5),
            "head_yaw": organic(t, 18, 0.18, 9),
            "head_roll": organic(t, 19, 0.16, 5),
            "spine_pitch": -3, "spine_roll": 0,
            "blend": {"Angry": 0.75, "BrowDown": 0.65}}

def smile(t):
    return {"head_pitch": organic(t, 20, 0.14, 4),
            "head_yaw": organic(t, 21, 0.1, 5.5),
            "head_roll": organic(t, 22, 0.12, 4),
            "spine_pitch": 0, "spine_roll": 0, "blend": {"Smile": 0.85}}

def lazy(t):
    return {"head_pitch": 19 + organic(t, 23, 0.06, 3),
            "head_yaw": organic(t, 24, 0.03, 6),
            "head_roll": 6 + organic(t, 25, 0.04, 3),
            "spine_pitch": 5, "spine_roll": 0, "blend": {"Smile": 0.05}}

def sad(t):
    return {"head_pitch": 15 + organic(t, 26, 0.06, 1.6),
            "head_yaw": organic(t, 27, 0.04, 4.5), "head_roll": -6,
            "spine_pitch": 4, "spine_roll": 0, "blend": {"Sad": 0.7}}

def sleepy(t):
    return {"head_pitch": 20 + organic(t, 28, 0.04, 4.5),
            "head_yaw": organic(t, 29, 0.024, 4.5), "head_roll": 8,
            "spine_pitch": 6, "spine_roll": 0, "blend": {}}

def surprised(t):
    return {"head_pitch": -11, "head_yaw": organic(t, 30, 0.18, 6.5),
            "head_roll": 0, "spine_pitch": -5, "spine_roll": 0,
            "blend": {"Surprised": 0.95, "BrowUp": 0.75}}

def smug(t):
    return {"head_pitch": -5, "head_yaw": 8 + organic(t, 31, 0.12, 4.5),
            "head_roll": 16, "spine_pitch": -3, "spine_roll": 0,
            "blend": {"Smug": 0.75, "Smile": 0.45}}

def nervous(t):
    return {"head_pitch": -3 + organic(t, 32, 0.28, 3.2),
            "head_yaw": organic(t, 33, 0.24, 10),
            "head_roll": organic(t, 34, 0.32, 4),
            "spine_pitch": -1, "spine_roll": 0,
            "blend": {"Sweat": 0.5, "Smile": 0.1}}

def bored(t):
    return {"head_pitch": 6 + organic(t, 35, 0.048, 3),
            "head_yaw": organic(t, 36, 0.04, 5),  # was 11 -- less side-to-side wander
            "head_roll": 4,
            "spine_pitch": 2, "spine_roll": 0, "blend": {}}

def alert(t):
    return {"head_pitch": -4, "head_yaw": organic(t, 37, 0.24, 19),
            "head_roll": 0, "spine_pitch": -3, "spine_roll": 0,
            "blend": {"Surprised": 0.35, "BrowUp": 0.35}}


STATE_FUNCS = {
    "neutral": neutral, "curious": curious, "questioning": questioning,
    "thinking": thinking, "listening": listening, "happy": happy, "shy": shy,
    "looking_around": looking_around, "daydreaming": daydreaming,
    "excited": excited, "angry": angry, "smile": smile, "lazy": lazy,
    "sad": sad, "sleepy": sleepy, "surprised": surprised, "smug": smug,
    "nervous": nervous, "bored": bored, "alert": alert,
}

ALL_BLENDS = set()
for _fn in STATE_FUNCS.values():
    try: ALL_BLENDS.update(_fn(0).get("blend", {}).keys())
    except Exception: pass
ALL_BLENDS.update(MOUTH_VISEMES)
ALL_BLENDS.update(["Blink_L", "Blink_R", "Blink"])


# ============================================================
# IDLE VARIATION
#
# FACE-FRONT MODE: dropped "looking_around" from the passive rotation
# -- it's a ~16 degree yaw swing that is *specifically designed* to
# look away from camera, and firing every 40-80s was the main source
# of the "wandering/haphazard" look. It's still fully available via
# REACT:/GESTURE: if you want it as a deliberate, triggered beat.
# daydreaming/bored stay in rotation for idle variety but their yaw
# amplitude has been trimmed above so they stay close to front-facing.
# Interval between variations also lengthened so idle stays calmer
# for longer stretches.
# ============================================================
_next_idle_variation  = time.time() + random.uniform(70, 130)
_idle_variation_state = None
_idle_variation_until = 0
IDLE_VARIATIONS = ["daydreaming", "bored"]

def maybe_pick_idle_variation(now):
    global _next_idle_variation, _idle_variation_state, _idle_variation_until
    if _idle_variation_state is not None and now < _idle_variation_until:
        return _idle_variation_state
    if _idle_variation_state is not None and now >= _idle_variation_until:
        _idle_variation_state = None
        _next_idle_variation  = now + random.uniform(70, 130)
    if now >= _next_idle_variation:
        _idle_variation_state = random.choice(IDLE_VARIATIONS)
        _idle_variation_until = now + random.uniform(3.0, 6.0)
        return _idle_variation_state
    return None


# ============================================================
# POSE SMOOTHING STATE
# ============================================================
_smooth_head_pitch  = 0.0
_smooth_head_yaw    = 0.0
_smooth_head_roll   = 0.0
_smooth_spine_pitch = 0.0
_smooth_spine_roll  = 0.0
_smooth_blends      = {}

current_state = "neutral"
_state_transition_start = 0.0
# FIX: this now stores the actual last-displayed pose (a full pose
# dict), not a state name. It's what the crossfade blends *from*.
# Storing the real displayed pose -- rather than re-evaluating
# STATE_FUNCS[<old state name>](t) at switch time -- is what makes
# interrupting a still-playing transition seamless instead of
# snapping back to the old state's pure pose first. See the state
# selection block in run_idle_loop() for how it's used.
_transition_start_pose = clamp_pose(STATE_FUNCS["neutral"](0))
_last_pose = _transition_start_pose


print("Idle Animation Started")
print("Emotion triggers: send  REACT:<emotion>[:<seconds>]  via stdin")
print("Gesture triggers: send  GESTURE:<name>[:<amp>]        via stdin")
print("Examples:  REACT:shocked   REACT:embarrassed:6   GESTURE:hand_raise:1.2")


def run_idle_loop():
    global current_state, _state_transition_start
    global _transition_start_pose, _last_pose
    global forced_state, forced_state_until
    global _smooth_head_pitch, _smooth_head_yaw, _smooth_head_roll
    global _smooth_spine_pitch, _smooth_spine_roll, _smooth_blends
    global _last_frame_time
    global _prev_hip_x, _smooth_hip_y

    current_state = "neutral"
    _state_transition_start = time.time()
    _transition_start_pose = clamp_pose(STATE_FUNCS["neutral"](0))
    _last_pose = _transition_start_pose

    while True:
        now = time.time()
        dt = min(now - _last_frame_time, 0.1)
        _last_frame_time = now

        pose_alpha  = _decay(POSE_HALF_LIFE, dt)
        blend_alpha = _decay(BLEND_HALF_LIFE, dt)
        talk_alpha  = _decay(TALK_HALF_LIFE, dt)

        # ── State selection ──
        if forced_state is not None and now < forced_state_until:
            new_state = forced_state
        else:
            if forced_state is not None and now >= forced_state_until:
                forced_state = None
                print("[react] returning to neutral")
            variation = maybe_pick_idle_variation(now)
            new_state = variation if variation else "neutral"

        if new_state != current_state:
            # Anchor the crossfade to whatever pose was actually on
            # screen last frame -- not a fresh evaluation of the old
            # state's function. If a react fires again while a
            # transition is still in flight (common: mood updates
            # after nearly every reply), re-anchoring to the old
            # state's raw pose would discard the blend already in
            # progress and pop back to that state's pure pose before
            # blending forward again. Anchoring to _last_pose makes
            # interrupting a transition seamless.
            _transition_start_pose = _last_pose
            current_state = new_state
            _state_transition_start = now

        t = now

        # ── Crossfade the pose functions themselves across a state
        # change, instead of only rate-limiting the bone value. This
        # is the fix for state-to-state popping/snapping. ──
        elapsed_since_switch = now - _state_transition_start
        new_pose = clamp_pose(STATE_FUNCS[current_state](t))
        if elapsed_since_switch < STATE_TRANSITION_TIME:
            bt = smootherstep(elapsed_since_switch / STATE_TRANSITION_TIME)
            pose = blend_poses(_transition_start_pose, new_pose, bt)
        else:
            pose = new_pose

        _last_pose = pose

        target_head_pitch  = pose["head_pitch"]
        target_head_yaw    = pose["head_yaw"]
        target_head_roll   = pose["head_roll"]
        target_spine_pitch = pose["spine_pitch"]
        target_spine_roll  = pose["spine_roll"]
        target_blends      = dict(pose["blend"])

        # ── Head drift (neutral only, camera-biased) ──
        if current_state == "neutral":
            dp, dy, dr = update_head_drift(now, dt)
            target_head_pitch += dp
            target_head_yaw   += dy
            target_head_roll  += dr

        # ── Breathing (always) ──
        target_spine_pitch += breathing(t, now, dt)

        # ── Blink ──
        b = blink_value(now, current_state)
        if b > 0.01:
            target_blends["Blink_L"] = b
            target_blends["Blink_R"] = b
            target_blends["Blink"]   = b

        # ── Talking overlay ──
        if is_talking:
            talk = talking_blend(t)
            target_head_pitch += talk["head_pitch_offset"]
            target_head_roll  += talk["head_roll_offset"]
            target_head_pitch = clamp(target_head_pitch, -20, 25)
            target_head_roll  = clamp(target_head_roll,  -18, 18)
            for k, v in talk["blend"].items():
                prev = _smooth_blends.get(k, 0.0)
                _smooth_blends[k] = lerp(prev, v, talk_alpha)
                target_blends[k]  = _smooth_blends[k]

        # ── Fidget layer ──
        maybe_fidget(now, current_state)

        # ── Smooth bones ──
        _smooth_head_pitch  = lerp_clamped(_smooth_head_pitch,  target_head_pitch,  pose_alpha, dt)
        _smooth_head_yaw    = lerp_clamped(_smooth_head_yaw,    target_head_yaw,    pose_alpha, dt)
        _smooth_head_roll   = lerp_clamped(_smooth_head_roll,   target_head_roll,   pose_alpha, dt)
        _smooth_spine_pitch = lerp_clamped(_smooth_spine_pitch, target_spine_pitch, pose_alpha, dt)
        _smooth_spine_roll  = lerp_clamped(_smooth_spine_roll,  target_spine_roll,  pose_alpha, dt)

        # ── Smooth blendshapes ──
        all_keys = ALL_BLENDS | set(target_blends.keys()) | set(_smooth_blends.keys())
        for name in all_keys:
            prev = _smooth_blends.get(name, 0.0)
            tgt  = target_blends.get(name, 0.0)
            a    = talk_alpha if (name in MOUTH_VISEMES and is_talking) else blend_alpha
            _smooth_blends[name] = lerp(prev, tgt, a)

        # ── Body sway + weight shift + occasional body turn ──
        sway_pitch, sway_roll, hip_roll, hip_x = update_body_sway(now, dt)
        body_yaw = update_body_turn(now, dt)
        final_spine_pitch = _smooth_spine_pitch + sway_pitch
        final_spine_roll  = _smooth_spine_roll  + sway_roll

        # ── Entrance sequence (peek-in / hold / retreat / walk-in) --
        # overrides hip_x entirely while active so it doesn't fight
        # with the normal idle weight-shift drift. ──
        ent_hip_x, ent_head_yaw, ent_spine_pitch, ent_blend, ent_legs, ent_active = compute_entrance(now)
        if ent_active:
            hip_x = ent_hip_x
            final_spine_pitch += ent_spine_pitch
            for k, v in ent_blend.items():
                _smooth_blends[k] = lerp(_smooth_blends.get(k, 0.0), v, blend_alpha)

        # ── Gesture layer ──
        # Raw envelope offsets get an extra short low-pass filter
        # (see smooth_gesture_offsets) before being added to any bone,
        # so overlapping/rapid gesture triggers don't pop.
        raw_gesture_offsets = compute_gesture_offsets(now)
        gesture_offsets = smooth_gesture_offsets(dt, raw_gesture_offsets)
        head_g  = gesture_offsets.get("Head",  (0.0, 0.0, 0.0))
        spine_g = gesture_offsets.get("Spine", (0.0, 0.0, 0.0))
        hips_g  = gesture_offsets.get("Hips",  (0.0, 0.0, 0.0))

        # ── Send bones ──
        # body_yaw (the slow occasional whole-body turn) is split
        # across head/spine/hips so the turn reads as one natural
        # pivot instead of a rigid single-axis rotation: head leads
        # slightly (0.5), spine follows partially (0.4), hips carry
        # the full turn (1.0) since the turn originates from the feet
        # up in a real body turn.
        send_bone("Head",  HEAD_POS,
                  _smooth_head_pitch + head_g[0],
                  _smooth_head_yaw   + head_g[1] + ent_head_yaw + body_yaw * 0.5,
                  _smooth_head_roll  + head_g[2])
        send_bone("Spine", SPINE_POS,
                  final_spine_pitch + spine_g[0],
                  body_yaw * 0.4,
                  final_spine_roll + spine_g[2])
        # hip_x carries the weight-shift lateral offset (meters), or the
        # entrance walk-in offset while that's active; hip rotation
        # still carries sway + the hip_pop gesture + the body turn.
        # Weight-shift "settle": a real body dips slightly as weight
        # actually lands when shifting from one leg to the other. hip_x
        # alone (pure lateral glide) reads as sliding on rails; this adds
        # a small, fast-decaying downward dip proportional to how fast
        # hip_x is currently moving, so a weight-shift transition has a
        # felt sense of gravity/landing instead of just translating.
        global _prev_hip_x, _smooth_hip_y
        hip_x_speed = abs(hip_x - _prev_hip_x) / max(dt, 1e-4)
        _prev_hip_x = hip_x
        settle_target = -min(HIP_SETTLE_MAX, hip_x_speed * HIP_SETTLE_STRENGTH * 0.001)
        settle_alpha = _decay(HIP_SETTLE_HALF_LIFE, dt)
        _smooth_hip_y += (settle_target - _smooth_hip_y) * settle_alpha

        send_bone("Hips", (hip_x, _smooth_hip_y, 0.0),
                  hips_g[0], hips_g[1] + body_yaw, hip_roll + hips_g[2])

        eye_pitch, eye_yaw = update_eyes(now, current_state)
        send_bone("LeftEye",  ZERO_POS, eye_pitch, eye_yaw, 0)
        send_bone("RightEye", ZERO_POS, eye_pitch, eye_yaw, 0)

        for bone, (p, y, r) in arm_bones(t, now, dt).items():
            g = gesture_offsets.get(bone, (0.0, 0.0, 0.0))
            send_bone(bone, ZERO_POS, p + g[0], y + g[1], r + g[2])

        # Only sent during the entrance walk-in -- this is the first
        # and only place in the script that drives leg bones at all.
        for bone, (p, y, r) in ent_legs.items():
            send_bone(bone, ZERO_POS, p, y, r)

        for name in ALL_BLENDS:
            send_blend(name, max(0.0, min(1.0, _smooth_blends.get(name, 0.0))))

        apply()
        time.sleep(1 / 60)


while True:
    try:
        run_idle_loop()
    except KeyboardInterrupt:
        print("[idle_animation] Caught KeyboardInterrupt - continuing idle loop.")
        time.sleep(0.1)
        continue
    except Exception as e:
        print(f"[idle_animation] Unexpected error in idle loop: {e} - restarting loop.")
        time.sleep(0.5)
        continue