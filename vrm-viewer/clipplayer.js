// ═══════════════════════════════════════════════════════════════
// clipPlayer.js  —  multi-clip library + response router
//
// USAGE in your main viewer:
//   import { ClipPlayer } from './clipPlayer.js';
//   const player = new ClipPlayer('/animations/clips/');
//   await player.loadAll();           // call once after VRM loads
//   player.play('idle_neutral');      // manual trigger
//
// The router is wired to your existing WebSocket messages from
// Lily.py.  Drop-in replacement for your getMocapFrame() usage.
// ═══════════════════════════════════════════════════════════════

// ── Clip manifest ────────────────────────────────────────────────────────
// Map each clip label to:
//   triggers  — WS message types / emotion names that activate it
//   priority  — higher wins when multiple triggers fire at once
//   minDur    — don't interrupt unless clip has played at least this many seconds
//   loop      — override loop flag from the recorded file (optional)
//   fallback  — what to fall back to when this clip ends (null = back to idle)
//
// Add a row for every clip you record.
const CLIP_MANIFEST = [
    // ── Idles (looping) ─────────────────────────────────────────────────────
    { label: 'idle_neutral', triggers: [], priority: 0, minDur: 0, loop: true, fallback: null },
    { label: 'idle_talking', triggers: ['talk_start'], priority: 5, minDur: 0, loop: true, fallback: 'idle_neutral' },

    // ── Reactive one-shots (return to idle when done) ───────────────────────
    { label: 'nod_agree', triggers: ['emotion:happy', 'intent:agree'], priority: 10, minDur: 0.5, loop: false, fallback: null },
    { label: 'nod_thinking', triggers: ['emotion:neutral', 'intent:thinking'], priority: 8, minDur: 0.5, loop: false, fallback: null },
    { label: 'shrug', triggers: ['intent:unsure', 'emotion:neutral'], priority: 9, minDur: 0.5, loop: false, fallback: null },
    { label: 'excited', triggers: ['emotion:excited'], priority: 12, minDur: 1.0, loop: false, fallback: 'idle_talking' },
    { label: 'laugh', triggers: ['emotion:happy', 'intent:laugh'], priority: 11, minDur: 1.0, loop: false, fallback: 'idle_neutral' },
    { label: 'sad', triggers: ['emotion:sad'], priority: 10, minDur: 1.0, loop: false, fallback: 'idle_neutral' },
    { label: 'surprised', triggers: ['emotion:surprised'], priority: 13, minDur: 0.3, loop: false, fallback: 'idle_neutral' },
    { label: 'shy', triggers: ['emotion:shy'], priority: 9, minDur: 0.5, loop: false, fallback: 'idle_neutral' },
    { label: 'thinking', triggers: ['intent:thinking'], priority: 7, minDur: 1.0, loop: true, fallback: 'idle_neutral' },
    { label: 'wave', triggers: ['intent:greet'], priority: 14, minDur: 1.0, loop: false, fallback: 'idle_neutral' },
];

// ── Helpers ───────────────────────────────────────────────────────────────
function lerp(a, b, t) { return a + (b - a) * t; }

function interpolateFrame(a, b, frac) {
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

function sampleClip(clip, localT) {
    const frames = clip.frames;
    if (!frames || frames.length < 2) return null;
    const dur = clip.duration;
    const t = clip.loop ? localT % dur : Math.min(localT, dur);
    let i = 0;
    for (; i < frames.length - 1; i++) { if (frames[i + 1].t > t) break; }
    const a = frames[i];
    const b = frames[Math.min(i + 1, frames.length - 1)];
    const dt = b.t - a.t;
    const frac = dt > 0 ? (t - a.t) / dt : 0;
    return interpolateFrame(a, b, frac);
}

// ── ClipPlayer class ──────────────────────────────────────────────────────
export class ClipPlayer {
    constructor(basePath = '/animations/clips/') {
        this.basePath = basePath;
        this.library = {};   // label → clip data
        this.manifest = CLIP_MANIFEST;

        this.current = null; // { label, startT, clip, entry }
        this.pending = null; // queued trigger waiting for minDur
        this.crossfadeT = 0;
        this.crossfadeDur = 0.25; // seconds to blend between clips
        this.prevFrame = null;

        // Build a trigger→entry map for fast lookup
        this._triggerMap = {};
        for (const entry of this.manifest) {
            for (const trigger of entry.triggers) {
                if (!this._triggerMap[trigger]) this._triggerMap[trigger] = [];
                this._triggerMap[trigger].push(entry);
            }
        }
    }

    // Load all clips whose JSON files exist in basePath.
    // Call this once after your VRM is ready.
    async loadAll() {
        const results = await Promise.allSettled(
            this.manifest.map(entry => this._loadClip(entry.label))
        );
        const loaded = results.filter(r => r.status === 'fulfilled').length;
        console.log(`[ClipPlayer] ${loaded}/${this.manifest.length} clips loaded`);

        // Start with idle_neutral
        this.play('idle_neutral');
        return loaded;
    }

    async _loadClip(label) {
        const url = `${this.basePath}${label}.json`;
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`${label} not found`);
        const data = await resp.json();
        // Support both raw frame array (old recorder) and new clip object format
        this.library[label] = Array.isArray(data)
            ? { label, duration: data[data.length - 1].t, frames: data, loop: label.startsWith('idle') }
            : data;
        console.log(`[ClipPlayer] Loaded: ${label} (${this.library[label].duration.toFixed(1)}s)`);
    }

    // Manually trigger a clip by label
    play(label, now = performance.now() / 1000) {
        const clip = this.library[label];
        if (!clip) { console.warn(`[ClipPlayer] Clip not found: ${label}`); return false; }
        const entry = this.manifest.find(e => e.label === label) || { loop: clip.loop, minDur: 0, fallback: null };

        // Capture current frame for crossfade
        if (this.current) {
            this.prevFrame = this._getCurrentFrame(now);
            this.crossfadeT = now;
        }

        this.current = { label, startT: now, clip, entry };
        this.pending = null;
        return true;
    }

    // Called from your WebSocket onmessage handler
    // msg = { type: 'talk_start' | 'talk_stop' | 'emotion' | 'intent', name?, value? }
    onWsMessage(msg, now = performance.now() / 1000) {
        let triggerKey = null;

        if (msg.type === 'talk_start') triggerKey = 'talk_start';
        else if (msg.type === 'talk_stop') {
            // Return to idle when talking stops
            this._returnToIdle(now);
            return;
        }
        else if (msg.type === 'emotion') triggerKey = `emotion:${msg.name}`;
        else if (msg.type === 'intent') triggerKey = `intent:${msg.name}`;

        if (!triggerKey) return;
        const candidates = this._triggerMap[triggerKey] || [];
        if (!candidates.length) return;

        // Pick highest priority candidate that has a loaded clip
        const best = candidates
            .filter(e => this.library[e.label])
            .sort((a, b) => b.priority - a.priority)[0];
        if (!best) return;

        // Don't interrupt current clip if it hasn't hit minDur yet
        const cur = this.current;
        if (cur && cur.entry.minDur > 0) {
            const elapsed = now - cur.startT;
            if (elapsed < cur.entry.minDur && best.priority <= cur.entry.priority) {
                this.pending = best;
                return;
            }
        }

        this.play(best.label, now);
    }

    _returnToIdle(now) {
        const isTalking = this.current?.label === 'idle_talking';
        if (isTalking) this.play('idle_neutral', now);
    }

    // Returns interpolated bone frame for current time — call every frame
    // Returns null if no clip is loaded/playing
    update(now = performance.now() / 1000) {
        if (!this.current) return null;
        const { clip, startT, entry } = this.current;
        const localT = now - startT;

        // Check if one-shot clip has ended
        if (!entry.loop && localT >= clip.duration) {
            const next = entry.fallback || 'idle_neutral';
            this.play(next, now);
            return this.update(now);
        }

        // Check pending clip (was waiting for minDur)
        if (this.pending && localT >= (this.current.entry.minDur || 0)) {
            this.play(this.pending.label, now);
            return this.update(now);
        }

        const frame = sampleClip(clip, localT);

        // Crossfade blend with previous clip
        if (this.prevFrame && this.crossfadeDur > 0) {
            const blend = Math.min(1, (now - this.crossfadeT) / this.crossfadeDur);
            if (blend < 1) {
                return this._blendFrames(this.prevFrame, frame, blend);
            } else {
                this.prevFrame = null;
            }
        }

        return frame;
    }

    _getCurrentFrame(now) {
        if (!this.current) return null;
        return sampleClip(this.current.clip, now - this.current.startT);
    }

    _blendFrames(a, b, t) {
        if (!a || !b) return b || a;
        const result = {};
        for (const bone of Object.keys(b)) {
            if (!a[bone]) { result[bone] = b[bone]; continue; }
            result[bone] = {
                x: lerp(a[bone].x, b[bone].x, t),
                y: lerp(a[bone].y, b[bone].y, t),
                z: lerp(a[bone].z, b[bone].z, t),
            };
        }
        return result;
    }

    get currentLabel() { return this.current?.label ?? null; }
}