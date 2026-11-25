"""Physical microphone onset logger; no audio persisted. Owner-authorized test only.
Start immediately before browser test. JSON timestamps can be correlated with browser
measurements. Onsets include *all* room sounds; they are not automatically assistant audio.
"""
import json
import time
import sys
import numpy as np
import sounddevice as sd
seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 60
started = time.time()
noise = 0.001
last = 0
rows = []
def capture(data, frames, timing, status):
    global noise, last
    rms = float(np.sqrt(np.mean(data ** 2)))
    now = time.time()
    if rms < max(noise * 2, 0.005):
        noise = noise * 0.98 + rms * 0.02
    if rms > max(0.008, noise * 4) and now - last > 0.3:
        rows.append(dict(wall_ms=round(now * 1000), rms=round(rms, 5)))
        last = now
with sd.InputStream(device='MacBook Pro Microphone', channels=1, samplerate=16000, blocksize=320, callback=capture):
    time.sleep(seconds)
print(json.dumps(dict(start_wall_ms=round(started*1000), duration_s=seconds, threshold_onsets=rows)))
