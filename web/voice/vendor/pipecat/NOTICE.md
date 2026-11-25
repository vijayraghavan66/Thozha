# Third-party provenance

`whisper_features.py` is copied unchanged from pipecat-ai/pipecat commit
`e2c83d45cc604d46293c83b41f563a7a16737f33`,
`src/pipecat/audio/turn/smart_turn/_whisper_features.py`.
Copyright (c) 2024–2026 Daily, BSD-2-Clause (`LICENSE`).
It contains Hugging Face/Librosa/Torchaudio derived feature extraction under
Apache-2.0 (`LICENSE-APACHE`); original notices are retained in the source.

The Smart Turn v3.2 int8 model is NOT committed. Local research setup copies it
from the same Pipecat revision to `~/.cache/thozha/voice/`.
SHA-256: `2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f`.
Model card: https://huggingface.co/pipecat-ai/smart-turn-v3 (BSD-2-Clause).
Inference preprocessing matches `LocalSmartTurnAnalyzerV3._predict_endpoint`:
last eight seconds, left padding, normalized Whisper log-mels, threshold 0.5.
No audio logging from the upstream optional debug path is included.

No LiveKit proprietary model or GPL Sonia source is incorporated.
