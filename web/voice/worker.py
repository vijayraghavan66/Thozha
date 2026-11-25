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
asr_slots = threading.BoundedSemaphore(2)


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
    if row['id'] in cancelled:
        return
    start = time.perf_counter()
    queue_ms = round(1000 * (start - row.get('_queued_at', start)))
    with httpx.Client(base_url='http://127.0.0.1:8000', trust_env=False, timeout=45,
                      headers={'Authorization': 'Bearer ' + credentials()}) as client:
        # Health is checked once per worker, not on every utterance. Its existing
        # backend health() warms the shared Whisper model; decoding is unchanged.
        if not getattr(transcribe, '_healthy', False):
            health = client.get('/v1/speech/health').raise_for_status().json()
            if not health.get('available') or health.get('backend') != 'faster-whisper':
                raise RuntimeError('Local faster-whisper unavailable; no cloud fallback')
            transcribe._healthy = True
        if row['id'] in cancelled:
            return
        decode_start = time.perf_counter()
        response = client.post('/v1/speech/transcribe', files={'file': ('voice.wav', data, 'audio/wav')}, data={'language': 'en'}).raise_for_status().json()
    if row['id'] not in cancelled:
        import resource
        emit(row['id'], text=response.get('text', '').strip(), metrics={
            'asr_worker_queue_ms': queue_ms,
            # Includes HTTP and backend queue/decode; API exposes no decode timer.
            'asr_upstream_roundtrip_ms': round(1000 * (time.perf_counter() - decode_start)),
            'asr_worker_ms': round(1000 * (time.perf_counter() - start)),
            'asr_input_sample_rate': 16000,
            'asr_input_samples': len(data[44:]) // 2,
            'voice_worker_peak_rss_mib': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576),
        }, done=True)


def reader():
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    for line in sys.stdin:
        row = {}
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
                if not asr_slots.acquire(blocking=False):
                    active.discard(id)
                    emit(id, error='Local transcription busy; retry after this turn.', done=True)
                    continue
                row['_queued_at'] = time.perf_counter()
                pool.submit(run_asr, row)
            else:
                requests.put_nowait(row)
        except Exception:
            rejected_id = row.get('id', '') if isinstance(row, dict) else ''
            active.discard(rejected_id)
            emit(rejected_id, error='Local voice request rejected', done=True)
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
        asr_slots.release()


def main():
    import numpy as np
    import soundfile as sf
    from mlx_audio.tts.utils import load_model
    model = load_model('mlx-community/Kokoro-82M-bf16')
    # Warm G2P and model once. Offline mode forbids surprise downloads.
    for _ in model.generate(text='Ready.', voice='af_bella', lang_code='a'):
        pass
    emit('ready', ready=True, engine='Kokoro-82M MLX', voices=['af_bella', 'am_adam'])
    expressive = None
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
            if not isinstance(text, str) or not 1 <= len(text) <= 400 or voice not in ('af_bella', 'am_adam', 'qwen_ryan'):
                raise ValueError('Invalid synthesis input')
            chunks = []
            rate = 24000
            if voice == 'qwen_ryan':
                if expressive is None:
                    expressive = load_model('mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit')
                generated = expressive.generate_custom_voice(
                    text=text, speaker='Ryan', language='English',
                    instruct='Speak warmly and naturally in a relaxed conversation. Use gentle varied intonation and thoughtful pauses. Do not exaggerate emotion.',
                    stream=True, streaming_interval=0.32)
            else:
                generated = model.generate(text=text, voice=voice, lang_code='a', speed=0.96)
            for result in generated:
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
