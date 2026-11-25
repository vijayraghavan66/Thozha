"""Offline inference benchmark of already-cached candidates; never downloads models."""
import os
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
import json
import resource
import sys
import time
from pathlib import Path
import numpy as np
import soundfile as sf
from mlx_audio.tts.utils import load_model

kind = sys.argv[1]
model_id = {'kokoro': 'mlx-community/Kokoro-82M-bf16', 'qwen': 'mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit'}[kind]
start = time.perf_counter()
model = load_model(model_id)
load_ms = (time.perf_counter() - start) * 1000
rows = []
for index in range(3):
    start = time.perf_counter(); first = None; chunks = []
    text = 'Hello, I am Thozha. What would you like to talk about today?'
    results = model.generate(text=text, voice='af_bella', lang_code='a') if kind == 'kokoro' else model.generate_custom_voice(text=text, speaker='Ryan', language='English', instruct='Speak warmly and conversationally.', stream=True, streaming_interval=.32)
    for result in results:
        chunk = np.asarray(result.audio).reshape(-1).astype(np.float32)
        if first is None: first = (time.perf_counter() - start) * 1000
        chunks.append(chunk.copy()); rate = result.sample_rate
    elapsed = (time.perf_counter() - start) * 1000
    waveform = np.concatenate(chunks)
    path = Path(__file__).parent / 'results' / f'{kind}-{index}.wav'
    path.parent.mkdir(exist_ok=True)
    sf.write(path, waveform, rate)
    row = {'engine': kind, 'run': index, 'load_ms': round(load_ms), 'first_chunk_ms': round(first), 'total_ms': round(elapsed), 'audio_ms': round(len(waveform) * 1000 / rate), 'peak_rss_mib': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576), 'sample': str(path)}
    rows.append(row); print(json.dumps(row), flush=True)
(Path(__file__).parent / 'results' / f'{kind}.json').write_text(json.dumps(rows, indent=2) + '\n')
