"""Local-only speech worker. Bounded JSON stdio; no listener or cloud fallback."""
from __future__ import annotations
import base64
import concurrent.futures
import io
import json
import os
import queue
import sys
import threading
import time
import tomllib
from pathlib import Path

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
# Libraries may print progress; stdout is exclusively the framed protocol.
protocol = sys.stdout
sys.stdout = sys.stderr
lock = threading.Lock()
requests: queue.Queue = queue.Queue(maxsize=3)
cancelled: set[str] = set()
active: set[str] = set()


def emit(id, **data):
    with lock:
        protocol.write(json.dumps({'id': id, **data}) + '\n')
        protocol.flush()


def credentials():
    # Same resolution as the native prototype; never return credentials over stdio.
    root = Path.home() / '.openjarvis'
    value = os.environ.get('OPENJARVIS_API_KEY', '')
    if not value:
        try:
            value = tomllib.loads((root / 'config.toml').read_text()).get('server', {}).get('auth', {}).get('api_key', '')
        except (OSError, ValueError):
            pass
    if not value:
        value = (root / 'supervisor_api_key').read_text().strip()
    if not value:
        raise RuntimeError('Local supervisor credential unavailable')
    return value


def transcribe(row):
    import httpx
    import wave
    data = base64.b64decode(row['audio'], validate=True)
    with wave.open(io.BytesIO(data)) as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000 or wav.getnframes() > 16000 * 31:
            raise ValueError('Expected bounded mono PCM16 WAV at 16 kHz')
    start = time.perf_counter()
    with httpx.Client(base_url='http://127.0.0.1:8000', trust_env=False, timeout=45,
                      headers={'Authorization': 'Bearer ' + credentials()}) as client:
        health = client.get('/v1/speech/health').raise_for_status().json()
        if not health.get('available') or health.get('backend') != 'faster-whisper':
            raise RuntimeError('Local faster-whisper unavailable; no cloud fallback')
        response = client.post('/v1/speech/transcribe', files={'file': ('voice.wav', data, 'audio/wav')}, data={'language': 'en'}).raise_for_status().json()
    if row['id'] not in cancelled:
        emit(row['id'], text=response.get('text', '').strip(), transcription_ms=round(1000 * (time.perf_counter() - start)), done=True)


def reader():
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    for line in sys.stdin:
        try:
            if len(line) > 1500000:
                continue
            row = json.loads(line)
            id = row['id']
            if row.get('action') == 'cancel':
                if id in active:
                    cancelled.add(id)
                continue
            if row.get('action') not in ('tts', 'stt'):
                raise ValueError('Unsupported action')
            active.add(id)
            if row['action'] == 'stt':
                # Admission is bounded by the Node adapter; ASR does not block TTS.
                pool.submit(run_asr, row)
            else:
                requests.put_nowait(row)
        except Exception:
            emit(row.get('id', ''), error='Local voice request rejected', done=True)
    requests.put(None)
    pool.shutdown(wait=False, cancel_futures=True)


def run_asr(row):
    try:
        transcribe(row)
    except Exception:
        emit(row['id'], error='Local transcription failed. Text chat remains available.', done=True)
    finally:
        active.discard(row['id'])
        cancelled.discard(row['id'])


def main():
    import numpy as np
    import soundfile as sf
    from mlx_audio.tts.utils import load_model
    model = load_model('mlx-community/Kokoro-82M-bf16')
    # Warm G2P and model once. Offline mode forbids surprise downloads.
    for _ in model.generate(text='Ready.', voice='af_bella', lang_code='a'):
        pass
    emit('ready', ready=True, engine='Kokoro-82M MLX', voices=['af_bella', 'am_adam'])
    threading.Thread(target=reader, daemon=True).start()
    while True:
        row = requests.get()
        if row is None:
            break
        id = row['id']
        try:
            if id in cancelled:
                continue
            start = time.perf_counter()
            text, voice = row['text'], row.get('voice', 'af_bella')
            if not isinstance(text, str) or not 1 <= len(text) <= 400 or voice not in ('af_bella', 'am_adam'):
                raise ValueError('Invalid synthesis input')
            chunks = []
            rate = 24000
            for result in model.generate(text=text, voice=voice, lang_code='a'):
                if id in cancelled:
                    break
                chunks.append(np.asarray(result.audio, dtype=np.float32).reshape(-1))
                rate = result.sample_rate
            if id not in cancelled and chunks:
                target = io.BytesIO()
                sf.write(target, np.concatenate(chunks), rate, format='WAV', subtype='PCM_16')
                emit(id, audio=base64.b64encode(target.getvalue()).decode(), synthesis_ms=round(1000 * (time.perf_counter() - start)), done=True)
        except Exception:
            emit(id, error='Local neural speech failed. No system or paid fallback was used.', done=True)
        finally:
            active.discard(id)
            cancelled.discard(id)


if __name__ == '__main__':
    main()
