"""Repeatable local semantic inference timing; synthetic/recorded != human acceptance."""
import base64
import json
from pathlib import Path
import subprocess
import time

python = str(Path.home() / '.openjarvis/src/.venv/bin/python')
# Run this harness with the existing speech environment (soundfile/scipy).
# The isolated ONNX worker continues using the backend's installed interpreter.
worker = subprocess.Popen([python, '-B', 'voice/semantic.py'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
try:
    ready = json.loads(worker.stdout.readline())
    assert ready.get('ready'), ready
    rows = []
    for name in ['voice2-kokoro-0.wav', 'voice2-kokoro-1.wav', 'voice2-qwen-0.wav']:
        # Convert explicitly synthetic neural listening samples to the actual input contract.
        import io
        import soundfile as sf
        import numpy as np
        from scipy.signal import resample_poly
        data, rate = sf.read(Path('voice/results') / name)
        import math
        divisor = math.gcd(rate, 16000)
        audio = resample_poly(data, 16000 // divisor, rate // divisor).astype(np.float32)
        for index in range(3):
            buf = io.BytesIO(); sf.write(buf, audio, 16000, format='WAV', subtype='PCM_16')
            started = time.perf_counter()
            worker.stdin.write(json.dumps({'id': str(index), 'audio': base64.b64encode(buf.getvalue()).decode()}) + '\n'); worker.stdin.flush()
            result = json.loads(worker.stdout.readline())
            rows.append({'fixture': name, 'kind': 'synthetic neural speech', 'roundtrip_ms': round(1000 * (time.perf_counter() - started)), **result})
    Path('voice/results/live-semantic.json').write_text(json.dumps(rows, indent=2))
    print(json.dumps(rows, indent=2))
finally:
    worker.terminate(); worker.wait(timeout=10)
