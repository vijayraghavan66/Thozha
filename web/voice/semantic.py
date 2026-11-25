"""Offline Pipecat Smart Turn v3.2 adapter; bounded stdio, no listening socket.
Feature extraction reused unchanged under BSD-2-Clause/Apache-2.0; see vendor/.
Model acquired explicitly from the inspected Pipecat checkout, never downloaded here.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import wave
import base64
import numpy as np
import onnxruntime as ort
from vendor.pipecat.whisper_features import compute_whisper_log_mel_features

MODEL_SHA256 = '2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f'


def features(audio):
    audio = np.asarray(audio, dtype=np.float32)[-128000:]
    audio = np.pad(audio, (max(0, 128000 - len(audio)), 0))
    return compute_whisper_log_mel_features(audio, do_normalize=True)[None, ...]


def main():
    path = Path(os.environ.get('THOZHA_TURN_MODEL', Path.home() / '.cache/thozha/voice/smart-turn-v3.2-cpu.onnx'))
    if hashlib.sha256(path.read_bytes()).hexdigest() != MODEL_SHA256:
        raise ValueError('Unrecognized semantic model')
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    model = ort.InferenceSession(str(path), options, providers=['CPUExecutionProvider'])
    model.run(None, {'input_features': features(np.zeros(16000, np.float32))})
    print(json.dumps({'id': 'ready', 'ready': True}), flush=True)
    for line in sys.stdin:
        row = {}
        try:
            if len(line) > 1450000:
                raise ValueError('Bound exceeded')
            row = json.loads(line)
            start = time.perf_counter()
            with wave.open(io.BytesIO(base64.b64decode(row['audio'], validate=True))) as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000) or wav.getnframes() > 31 * 16000:
                    raise ValueError('Expected bounded PCM16')
                audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(np.float32) / 32768
            probability = float(model.run(None, {'input_features': features(audio)})[0].reshape(-1)[0])
            print(json.dumps({'id': row['id'], 'probability': probability, 'complete': probability > 0.5,
                              'semantic_ms': round(1000 * (time.perf_counter() - start)), 'done': True}), flush=True)
        except Exception:
            print(json.dumps({'id': row.get('id', ''), 'error': 'Semantic turn inference unavailable', 'done': True}), flush=True)


if __name__ == '__main__':
    main()
