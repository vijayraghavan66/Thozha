"""Generate offline A/B listening samples. No subjective quality score is fabricated."""
import os
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
import json
import resource
import time
from pathlib import Path
import numpy as np
import soundfile as sf
from mlx_audio.tts.utils import load_model

texts = [
    "Of course. Take your time, I'm listening. What would you like to explore?",
    "That sounds frustrating. Let's work through it together, one step at a time.",
    "Good news! The checks passed. There's just one small detail we should review before moving on.",
]
rows = []
for kind, model_id in [('kokoro', 'mlx-community/Kokoro-82M-bf16'), ('qwen', 'mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit')]:
    model = load_model(model_id)
    for index, text in enumerate(texts):
        started = time.perf_counter()
        first = None
        chunks = []
        generation = model.generate(text=text, voice='af_bella', lang_code='a', speed=0.96) if kind == 'kokoro' else model.generate_custom_voice(text=text, speaker='Ryan', language='English', instruct='Speak warmly and naturally in a relaxed conversation. Use gentle varied intonation and thoughtful pauses. Do not exaggerate emotion.', stream=True, streaming_interval=0.32)
        for item in generation:
            if first is None:
                first = (time.perf_counter() - started) * 1000
            chunks.append(np.asarray(item.audio).reshape(-1).copy())
            rate = item.sample_rate
        target = Path('voice/results') / f'voice2-{kind}-{index}.wav'
        audio = np.concatenate(chunks)
        sf.write(target, audio, rate)
        row = dict(engine=kind, sample=str(target), first_chunk_ms=round(first), total_ms=round((time.perf_counter()-started)*1000), duration_ms=round(len(audio)*1000/rate), peak_process_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1048576))
        rows.append(row)
        print(json.dumps(row), flush=True)
    del model
Path('voice/results/voice2-listening.json').write_text(json.dumps(rows, indent=2))
