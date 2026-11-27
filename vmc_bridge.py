#!/usr/bin/env python3
"""
VMC Protocol → WebSocket bridge for Lily.

Receives OSC bone/blendshape data from XR Animator (UDP 39539)
and forwards it to the Three.js client via WebSocket (8765).

Install:
    pip install python-osc asyncio websockets

Run:
    python vmc_bridge.py

Then in XR Animator (Electron mode):
    Enable VMC protocol output → send to 127.0.0.1:39539
"""

import asyncio
import json
import time
from collections import defaultdict

# ── Config ──────────────────────────────────────────────
VMC_PORT      = 39539      # UDP port XR Animator sends to
WS_HOST       = "127.0.0.1"
WS_PORT       = 8765       # WebSocket port your renderer connects to
SEND_RATE_HZ  = 60         # Max frames/sec forwarded to browser
# ────────────────────────────────────────────────────────

# ── Unity HumanBodyBones → VRM bone name mapping ──
# XR Animator sends Unity names; three-vrm uses these VRM names.
BONE_MAP = {
    "Hips":             "hips",
    "Spine":            "spine",
    "Chest":            "chest",
    "UpperChest":       "upperChest",
    "Neck":             "neck",
    "Head":             "head",
    "LeftShoulder":     "leftShoulder",
    "LeftUpperArm":     "leftUpperArm",
    "LeftLowerArm":     "leftLowerArm",
    "LeftHand":         "leftHand",
    "RightShoulder":    "rightShoulder",
    "RightUpperArm":    "rightUpperArm",
    "RightLowerArm":    "rightLowerArm",
    "RightHand":        "rightHand",
    "LeftUpperLeg":     "leftUpperLeg",
    "LeftLowerLeg":     "leftLowerLeg",
    "LeftFoot":         "leftFoot",
    "LeftToes":         "leftToes",
    "RightUpperLeg":    "rightUpperLeg",
    "RightLowerLeg":    "rightLowerLeg",
    "RightFoot":        "rightFoot",
    "RightToes":        "rightToes",
    "LeftEye":          "leftEye",
    "RightEye":         "rightEye",
    "Jaw":              "jaw",
    # Fingers
    "LeftThumbProximal":       "leftThumbMetacarpal",
    "LeftThumbIntermediate":   "leftThumbProximal",
    "LeftThumbDistal":         "leftThumbDistal",
    "LeftIndexProximal":       "leftIndexProximal",
    "LeftIndexIntermediate":   "leftIndexIntermediate",
    "LeftIndexDistal":         "leftIndexDistal",
    "LeftMiddleProximal":      "leftMiddleProximal",
    "LeftMiddleIntermediate":  "leftMiddleIntermediate",
    "LeftMiddleDistal":        "leftMiddleDistal",
    "LeftRingProximal":        "leftRingProximal",
    "LeftRingIntermediate":    "leftRingIntermediate",
    "LeftRingDistal":          "leftRingDistal",
    "LeftLittleProximal":      "leftLittleProximal",
    "LeftLittleIntermediate":  "leftLittleIntermediate",
    "LeftLittleDistal":        "leftLittleDistal",
    "RightThumbProximal":      "rightThumbMetacarpal",
    "RightThumbIntermediate":  "rightThumbProximal",
    "RightThumbDistal":        "rightThumbDistal",
    "RightIndexProximal":      "rightIndexProximal",
    "RightIndexIntermediate":  "rightIndexIntermediate",
    "RightIndexDistal":        "rightIndexDistal",
    "RightMiddleProximal":     "rightMiddleProximal",
    "RightMiddleIntermediate": "rightMiddleIntermediate",
    "RightMiddleDistal":       "rightMiddleDistal",
    "RightRingProximal":       "rightRingProximal",
    "RightRingIntermediate":   "rightRingIntermediate",
    "RightRingDistal":         "rightRingDistal",
    "RightLittleProximal":     "rightLittleProximal",
    "RightLittleIntermediate": "rightLittleIntermediate",
    "RightLittleDistal":       "rightLittleDistal",
}

# ── Shared state (written by OSC receiver, read by WS sender) ──
latest_bones = {}        # { vrm_bone_name: { qx, qy, qz, qw } }
latest_blendshapes = {}  # { name: value }
root_pos = None          # { x, y, z, qx, qy, qz, qw }
frame_dirty = False

# ── OSC / UDP receiver ─────────────────────────────────
try:
    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_server import AsyncIOOSCUDPServer
except ImportError:
    print("Install python-osc:  pip install python-osc")
    raise SystemExit(1)


def on_bone(address, *args):
    """Handle /VMC/Ext/Bone/Pos — bone_name px py pz qx qy qz qw"""
    global frame_dirty
    if len(args) < 8:
        return
    unity_name = args[0]
    vrm_name = BONE_MAP.get(unity_name)
    if vrm_name is None:
        return
    latest_bones[vrm_name] = {
        "qx": round(args[4], 5),
        "qy": round(args[5], 5),
        "qz": round(args[6], 5),
        "qw": round(args[7], 5),
    }
    frame_dirty = True


def on_root(address, *args):
    """Handle /VMC/Ext/Root/Pos — name px py pz qx qy qz qw"""
    global root_pos, frame_dirty
    if len(args) < 8:
        return
    root_pos = {
        "x":  round(args[1], 5),
        "y":  round(args[2], 5),
        "z":  round(args[3], 5),
        "qx": round(args[4], 5),
        "qy": round(args[5], 5),
        "qz": round(args[6], 5),
        "qw": round(args[7], 5),
    }
    frame_dirty = True


def on_blendshape(address, *args):
    """Handle /VMC/Ext/Blend/Val — name value"""
    global frame_dirty
    if len(args) < 2:
        return
    latest_blendshapes[args[0]] = round(args[1], 4)
    frame_dirty = True


def on_blendshape_apply(address, *args):
    """Handle /VMC/Ext/Blend/Apply — signals all blendshapes sent this tick."""
    pass  # We send on the WS tick, not per OSC packet


# ── WebSocket sender ───────────────────────────────────
try:
    import websockets
    from websockets.asyncio.server import serve as ws_serve
except ImportError:
    print("Install websockets:  pip install websockets")
    raise SystemExit(1)

connected_clients = set()


async def ws_handler(websocket):
    global connected_clients
    connected_clients.add(websocket)  # noqa: F821
    remote = websocket.remote_address
    print(f"[WS] Client connected: {remote}")
    try:
        async for msg in websocket:
            # Handle messages from the renderer (shutdown, etc.)
            try:
                data = json.loads(msg)
                if data.get("type") == "shutdown":
                    await websocket.send(json.dumps({"type": "shutdown_ack"}))
                    print("[WS] Shutdown requested")
            except json.JSONDecodeError:
                pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        connected_clients.discard(websocket)
        print(f"[WS] Client disconnected: {remote}")


async def broadcast_loop():
    """Send bone/blendshape state to all connected browsers at SEND_RATE_HZ."""
    global frame_dirty
    interval = 1.0 / SEND_RATE_HZ

    while True:
        if frame_dirty and connected_clients:
            payload = {
                "type": "vmc_frame",
                "bones": latest_bones,
            }
            if root_pos:
                payload["root"] = root_pos
            if latest_blendshapes:
                payload["blendshapes"] = dict(latest_blendshapes)

            msg = json.dumps(payload)
            dead = set()
            for ws in connected_clients:
                try:
                    await ws.send(msg)
                except Exception:
                    dead.add(ws)
            connected_clients -= dead
            frame_dirty = False

        await asyncio.sleep(interval)


# ── Main ───────────────────────────────────────────────
async def main():
    # OSC dispatcher
    disp = Dispatcher()
    disp.map("/VMC/Ext/Bone/Pos",     on_bone)
    disp.map("/VMC/Ext/Root/Pos",     on_root)
    disp.map("/VMC/Ext/Blend/Val",    on_blendshape)
    disp.map("/VMC/Ext/Blend/Apply",  on_blendshape_apply)

    # Start OSC/UDP server
    osc_server = AsyncIOOSCUDPServer(
        ("0.0.0.0", VMC_PORT), disp, asyncio.get_event_loop()
    )
    osc_transport, osc_protocol = await osc_server.create_serve_endpoint()
    print(f"[OSC] Listening on UDP :{VMC_PORT}  (waiting for XR Animator)")

    # Start WebSocket server
    ws_server = await ws_serve(ws_handler, WS_HOST, WS_PORT)
    print(f"[WS]  Listening on ws://{WS_HOST}:{WS_PORT}")

    # Start broadcast loop
    await broadcast_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")