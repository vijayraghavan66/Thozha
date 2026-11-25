# Thozha continuous Web voice — isolated development integration

Branch: `sahaa/web-conversational-voice`, based on `copilot/web@16c03db1`.
No active backend, gateway, Android, certificate, pairing or LaunchAgent changes.

## Open

The isolated preview runs at **http://127.0.0.1:5176/#/ai-workspace**.
Use your existing Web owner sign-in. Select or create a conversation, then **Voice**.
Allow the browser microphone prompt. Speak, pause, and wait for the response.
The microphone stays active until End/Escape. Mute disables its capture track.
Alt-M mutes; Alt-I interrupts; Escape ends. Use headphones for reliable barge-in.
Voice selection: Bella or Adam. Output selection appears when AudioContext.setSinkId
is supported. Changing voice affects subsequent synthesis, not an already buffered sentence.

Start from this directory's parent (`web/`) if the preview is not running:

```sh
THOZHA_VOICE_DEV=1 THOZHA_WEB_UPSTREAM=http://127.0.0.1:5174 npm run dev -- --port 5176
```

Requires the existing authenticated Web frontend on 5174 and backend on 8000.
The worker defaults to the already installed evaluation Python environment; override
with `THOZHA_VOICE_PYTHON` if needed. No packages or weights are installed by this feature.
All neural loading runs with Hugging Face/Transformers offline mode. Missing artifacts
fail closed; no paid-provider, system-speech or browser cloud-recognition fallback.

**Deployment boundary:** the voice adapter is a Vite development plugin, not a production
backend deployment. The static build alone does not serve speech. Backend owner should
mount equivalent bounded handlers behind the existing Web session/CSRF dependency before
production integration. The adapter only exists when THOZHA_VOICE_DEV=1. It validates the
existing session via 5174, exact Host/Origin and CSRF. The development bridge rewrites only
validated same-origin Web requests to the existing configured canonical origin. It does
not change that backend's origin policy or expose raw API routes. Never bind it to a LAN IP.

## Pipeline and boundaries

- Browser AudioWorklet: ~20 ms mono capture blocks; getUserMedia echo cancellation,
  noise suppression, AGC. Capture connects to a zero-gain output, never microphone monitoring.
- Adaptive energy VAD: onset debounce, playback-specific higher threshold, 620 ms endpoint
  silence, minimum voiced duration and 29-second utterance cap, ~240 ms pre-roll.
  This is an energy detector, **not a neural semantic endpoint model**.
- During transcription/thinking, energy alone does not cancel the in-flight request;
  interrupt button is available. During actual playback, sustained speech stops playback,
  invalidates synthesis and cancels the associated conversation response.
- Local faster-whisper through the existing authenticated speech API. It is whole-turn
  ASR and does not offer partial transcripts. Audio never goes to the model provider.
- Existing owner-authenticated Web conversation API owns chat history and engine generation.
  The deployed configuration identifies GPT-6 Luna. Web chat has tools disabled; sensitive
  actions remain in the existing reviewed task/approval UI. No new tool-execution path.
- SSE modes are honored: sentence synthesis can start early only for `engine_stream`.
  Buffered engines must finish first; no claim of true Luna token streaming.
- Local MLX Kokoro 82M, warmed once per speech worker, sentence-level WAV chunks.
  Next sentence synthesis overlaps playback; one-sentence lookahead bounds decoded audio.
- A persisted per-tab request ID and content precede submission. Recovery uses that key.
  Refresh stops capture; it NEVER reacquires a microphone without another explicit Voice click.
  Pending transcript/key are plaintext in sessionStorage, cleared after normal completion.
- Raw recordings are not persisted. The Python worker holds bounded audio in memory only.
  Local benchmark/test WAV and diagnostics are ignored in `voice/results/`.

## Current validation

Build, TypeScript, lint and 42 unit/regression tests pass. Voice-specific tests cover VAD,
WAV conversion, sentence splitting, shutdown, mute, cancellation, stale transcripts,
uncertain submission and Host/Origin/CSRF requirements.

Real Chromium, physical default built-in Mac microphone, owner-spoken turns observed:

1. “What is Java?” → genuine answer → local neural playback.
2. “What do they use it for?” → context-preserving genuine answer → playback.

Earlier real-microphone run also exercised interrupt and mute/unmute controls. Synthetic
browser input separately completed two consecutive turns, two submissions, no page errors.
A real acoustic speaker-loopback stress run FAILED reliable turn delivery: room energy
repeatedly interrupted ASR. The implementation was changed to suppress energy-only
interruptions while processing and to reject short voiced bursts. Do not represent that
failed run as acceptance. Natural spoken barge-in/echo robustness still needs owner review.

Real-microphone sample timing, measured from estimated speech end (includes endpoint delay):

| Metric | Turn 1 | Turn 2 |
|---|---:|---:|
| Final transcript | 1365 ms | 1046 ms |
| AI acceptance | 1444 ms | 1062 ms |
| Complete response | 8589 ms | 7664 ms |
| First audio scheduled | 9345 ms | 9648 ms |
| AudioContext output latency | 5 ms | 5 ms |

One interrupt reported 4 ms local stop-call latency. This excludes VAD onset/debounce,
OS output buffers and acoustic detection. Scheduled audio is NOT independently measured
time-to-first-audible sound. These samples are not percentiles or performance guarantees.
The current 9–10 second wait does not yet meet an effortless real-time conversation goal.

## Candidate measurements from this implementation session

Same cached-model English greeting, three runs each; results in ignored results/*.json.

| Candidate | Model load | First generated audio cold / warm | Peak process RSS |
|---|---:|---:|---:|
| Kokoro-82M MLX Bella | 174 ms | 5994 / 178–181 ms | 376 MiB |
| Qwen3-TTS 0.6B 8-bit Ryan | 3319 ms | 591 / 199–286 ms | 1112 MiB |

Kokoro generated 4.175 s audio in 185–188 ms warm. Qwen generated 4.96–6.16 s in
2.07–2.59 s warm and supports genuine small streaming chunks. Kokoro selected for throughput,
RAM and existing cached voices, not a proven subjective naturalness win. Owner listening
review remains necessary. Piper is not installed; not benchmarked or silently installed.
Kokoro common distribution/model license is Apache-2.0; verify pinned distribution notices
before shipping. Qwen exact model/runtime notices likewise require redistribution review.

## Reproduce tests

```sh
npm run build
npm run lint
npm test
node voice/browser-check.mjs
node voice/call-check.mjs        # synthetic media; real authenticated model/API
node voice/recovery-check.mjs    # synthetic input; real reconnect/history/idempotency
node voice/live-acceptance.mjs   # physical mic; owner permission and speech required
```

`call-check.mjs` requires `results/input.wav`; it is generated during the initial test run,
not committed. Test harnesses read the existing mode-0600 owner password only for sign-in,
never print it or retain browser auth state. They create normal conversation history and
provider calls through the existing configured AI path. Neural speech benchmarks are local.

## Remaining acceptance / work

- Natural owner barge-in under speaker echo, complete answer playback and selected voice
  quality need extended acceptance; energy VAD is not production-grade acoustic separation.
- Real acoustic first-audible latency still unmeasured; UI timing is scheduling only.
- Browser suspension, lengthy offline recovery, Safari/iOS and memory pressure unverified.
- No new model-provider token streaming, partial ASR or paid fallback is added.
- Production authenticated endpoint integration needs backend-owner review, not a live restart.
- A cold speech worker needs warmup before listening. Current app requests mic before warmup
  and displays connecting status; End still stops tracks during warmup.
