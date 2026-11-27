"""
talk_driver.py

Plays a TTS-generated audio file out loud and sends live volume to the
animation process for lipsync. Robust against multi-channel output
devices (e.g. a 16-channel VB-Cable) which otherwise cause
[Errno 22] Invalid argument from sounddevice.

Resamples audio to a standard 48000 Hz before playback and requests
high-latency buffering from PortAudio, since GPT-SoVITS output
(commonly 32000 Hz) played at a non-native device rate without enough
buffer headroom causes audible crackling/static on Windows.

Lipsync amplitude is driven from inside the PortAudio output callback
(sd.OutputStream) rather than a separate wall-clock timer, so it can't
drift out of sync the way a timer started alongside sd.play() would.

IMPORTANT TIMING NOTE: PortAudio's output callback fires as it
*requests* frames, which is slightly *ahead* of when those frames are
actually audible -- with latency="high" that lead time is commonly
200-300ms on Windows. Sending TALK_AMP the instant the callback fires
would send every amplitude value that much early, so mouth movement
would consistently lead the real audio -- most noticeable at the end
of a line, where the fading/quiet trailing values arrive while the
last audible words are still playing, closing the mouth before the
sound actually stops. To fix this, each amplitude value is queued with
its arrival timestamp, and a small scheduler thread delays the actual
TALK_AMP send by the stream's own reported output latency so it lines
up with what's really heard.

External sync (VMC + browser viewer both driven from one clock):
MovementController accepts optional on_amplitude/on_talk_start/
on_talk_stop callbacks. These fire from the *same* latency-compensated
scheduler that drives the stdin TALK_AMP/TALK_START/TALK_STOP commands
to idle_animation.py (VMC), so any other consumer (e.g. the browser VRM
viewer via ws_server.notify_talk_amp) stays in sync with VMC instead of
running its own independent timing loop.
"""

import os
import queue
import subprocess
import threading
import time
from typing import Optional

import numpy as np
import sounddevice as sd
import soundfile as sf
import soxr

TARGET_SAMPLE_RATE = 48000


def _find_vbcable_input_device() -> Optional[int]:
    try:
        devices = sd.query_devices()
        for idx, dev in enumerate(devices):
            if isinstance(dev, dict):
                d = dev
            else:
                try:
                    d = dict(dev)
                except Exception:
                    continue
            name = str(d.get("name", ""))
            max_out = int(d.get("max_output_channels", 0))
            if max_out > 0 and ("CABLE Input" in name or "CABLE In" in name or "VB-Audio Virtual Cable" in name):
                return idx
    except Exception as e:
        print(f"[talk_driver] Error finding VB-Cable device: {e}")
    return None


def _device_output_channels(device_idx) -> int:
    if device_idx is None:
        return 2
    try:
        info = dict(sd.query_devices(device_idx))
        return max(1, int(info.get("max_output_channels", 2)))
    except Exception:
        return 2


class MovementController:

    def __init__(self, movement_script_path, python_exe="python", use_vbcable=False,
                 on_amplitude=None, on_talk_start=None, on_talk_stop=None):
        # on_amplitude(value: float), on_talk_start(), on_talk_stop() are
        # optional callbacks fired alongside the stdin TALK_AMP/TALK_START/
        # TALK_STOP commands sent to idle_animation.py (VMC). This lets an
        # external system -- e.g. ws_server.notify_talk_amp for the browser
        # VRM viewer -- stay perfectly in sync with VMC instead of running
        # its own separate, independently-timed lipsync loop.
        self._on_amplitude = on_amplitude
        self._on_talk_start = on_talk_start
        self._on_talk_stop = on_talk_stop

        # NOTE: creationflags=subprocess.CREATE_NEW_PROCESS_GROUP is critical here.
        # Without it, this child process shares the parent console's signal
        # group on Windows, so any Ctrl+C / KeyboardInterrupt that hits the
        # parent (e.g. from a different subprocess failing to launch) also
        # kills idle_animation.py silently in the background. That freezes
        # Lily (no blink, no sway) while lily_bot.py itself keeps running,
        # since lily_bot.py installs its own SIGINT handler that just warns
        # instead of exiting. Isolating the process group fixes this.
        self.proc = subprocess.Popen(
            [python_exe, movement_script_path],
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

        # use_vbcable=False (the default) means Lily's voice plays on your
        # normal default speakers/headphones, so you can actually hear her.
        # Set use_vbcable=True only once you're ready to stream and want her
        # voice routed into OBS/Discord instead of your own speakers - with
        # it on, you will NOT hear her locally unless you separately add an
        # Audio Input Capture (CABLE Output) source in OBS and monitor it.
        if use_vbcable:
            self._vbcable_device = _find_vbcable_input_device()
            self._out_channels = _device_output_channels(self._vbcable_device)
            if self._vbcable_device is None:
                print("[talk_driver] WARNING: VB-Cable device not found - "
                      "using default output device instead.")
            else:
                print(f"[talk_driver] Routing Lily's voice to VB-Cable "
                      f"(device #{self._vbcable_device}, {self._out_channels} ch). "
                      f"You will NOT hear her locally unless OBS is "
                      f"monitoring CABLE Output.")
        else:
            self._vbcable_device = None
            self._out_channels = 2
            print("[talk_driver] Playing Lily's voice on your default output "
                  "device (speakers/headphones).")

    def _send(self, command):
        if self.proc.stdin and not self.proc.stdin.closed:
            try:
                self.proc.stdin.write(command + "\n")
                self.proc.stdin.flush()
            except Exception:
                pass

    def set_state(self, name, duration=4.0):
        self._send(f"STATE:{name}:{duration}")

    def talk_start(self):
        self._send("TALK_START")
        if self._on_talk_start:
            try:
                self._on_talk_start()
            except Exception:
                pass

    def talk_stop(self):
        self._send("TALK_STOP")
        if self._on_talk_stop:
            try:
                self._on_talk_stop()
            except Exception:
                pass

    def talk_amplitude(self, value):
        self._send(f"TALK_AMP:{value:.3f}")
        if self._on_amplitude:
            try:
                self._on_amplitude(value)
            except Exception:
                pass

    def speak(self, audio_path, block=True):
        if block:
            self._speak_blocking(audio_path)
        else:
            threading.Thread(
                target=self._speak_blocking,
                args=(audio_path,),
                daemon=True
            ).start()

    def _speak_blocking(self, audio_path):
        try:
            data, samplerate = sf.read(audio_path, dtype="float32")
        except Exception as e:
            print(f"[talk_driver] Could not read '{audio_path}': {e}")
            return

        # Mono signal for both analysis and as the playback source.
        mono = data.mean(axis=1) if data.ndim > 1 else data
        mono = np.ascontiguousarray(mono, dtype="float32")

        # Resample to a standard rate before playback. GPT-SoVITS commonly
        # outputs 32000 Hz, which isn't the native rate of most Windows
        # audio devices - letting the OS resample in real time with tight
        # buffers is what causes crackling/static. Resampling ourselves to
        # 48000 Hz avoids that entirely.
        if samplerate != TARGET_SAMPLE_RATE:
            mono = soxr.resample(mono, samplerate, TARGET_SAMPLE_RATE)
            mono = np.ascontiguousarray(mono, dtype="float32")
            samplerate = TARGET_SAMPLE_RATE

        total = len(mono)
        blocksize = max(1, samplerate // 30)
        pos = [0]  # mutable closure state, advanced inside the callback

        def make_callback(n_channels, amp_queue):
            def callback(outdata, frames, time_info, status):
                start = pos[0]
                end = min(start + frames, total)
                n = end - start

                chunk = mono[start:end]
                if n_channels >= 1:
                    outdata[:n, 0] = chunk
                if n_channels >= 2:
                    outdata[:n, 1] = chunk
                if n_channels > 2:
                    outdata[:n, 2:] = 0.0
                if n < frames:
                    outdata[n:] = 0.0

                # Queue the value with its arrival time rather than
                # sending it immediately -- see module docstring for why.
                # The scheduler thread (started in try_stream) delays the
                # actual send by the stream's output latency.
                if n > 0:
                    rms = float(np.sqrt(np.mean(chunk ** 2)))
                    amp_queue.put((time.time(), min(1.0, rms * 6.0)))

                pos[0] = end
                if end >= total:
                    raise sd.CallbackStop()
            return callback

        def try_stream(device, n_channels):
            pos[0] = 0
            amp_queue: "queue.SimpleQueue" = queue.SimpleQueue()
            stop_scheduler = threading.Event()

            def scheduler_worker(latency_delay: float):
                while True:
                    try:
                        arrival_time, value = amp_queue.get(timeout=0.05)
                    except queue.Empty:
                        if stop_scheduler.is_set():
                            return
                        continue
                    due = arrival_time + latency_delay
                    now = time.time()
                    if due > now:
                        time.sleep(due - now)
                    self.talk_amplitude(value)

            with sd.OutputStream(
                samplerate=samplerate,
                channels=n_channels,
                dtype="float32",
                blocksize=blocksize,
                latency="high",
                device=device,
                callback=make_callback(n_channels, amp_queue),
            ) as stream:
                try:
                    latency_val = stream.latency
                    latency_delay = float(latency_val[0] if isinstance(latency_val, tuple) else latency_val)
                except Exception:
                    latency_delay = 0.15
                latency_delay = max(latency_delay, 0.05)

                scheduler_thread = threading.Thread(
                    target=scheduler_worker, args=(latency_delay,), daemon=True
                )
                scheduler_thread.start()

                while pos[0] < total and stream.active:
                    time.sleep(0.01)

                # Let the scheduler drain any amplitude values still
                # queued/delayed (up to ~latency_delay worth) before
                # returning, so talk_stop() (mouth close) doesn't fire
                # until the real audio has actually finished playing.
                stop_scheduler.set()
                scheduler_thread.join(timeout=latency_delay + 0.5)
                time.sleep(0.05)

        self.talk_start()

        played = False
        if self._vbcable_device is not None:
            try:
                try_stream(self._vbcable_device, self._out_channels)
                played = True
            except Exception as e:
                print(f"[talk_driver] VB-Cable playback failed ({e}); "
                      f"trying default output.")

        if not played:
            try:
                try_stream(None, 2)
                played = True
            except Exception as e:
                print(f"[talk_driver] Default stereo playback failed ({e}); "
                      f"trying mono.")

        if not played:
            try:
                try_stream(None, 1)
                played = True
            except Exception as e:
                print(f"[talk_driver] All playback attempts failed: {e}")

        self.talk_stop()

    def close(self):
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
        self.proc.terminate()


if __name__ == "__main__":
    import sys
    movement_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "idle_animation.py")
    audio_file = sys.argv[1] if len(sys.argv) > 1 else "reply.wav"
    controller = MovementController(movement_path)
    print(f"Speaking: {audio_file}")
    controller.speak(audio_file)
    print("Done speaking.")
    time.sleep(2)
    controller.close()