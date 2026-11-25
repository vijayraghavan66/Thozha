# Recognition regression repair — 2026-11-25

Status: preview repair, NOT physical accuracy acceptance. All edits are confined to the existing voice worktree. Pre-existing uncommitted Voice 2 changes are preserved. No gateway/supervisor restart or configuration change.

## Confirmed implementation defects

1. `@ricky0123/vad-web/dist/real-time-vad.js:getVADNodeAsWorklet` uses an async MessagePort callback and awaits processFrame independently for each message. MessagePort does not await earlier callbacks. `models/v5.js:process` reads recurrent `_state`, awaits ONNX session.run, then replaces `_state`. Overlapping calls can therefore use stale state. This is a source-confirmed race; its frequency/contribution to owner WER is not measured. `src/voice/inference.ts` serializes inference in arrival order, bounds backlog at 64 frames (2.048 seconds), and ends capture visibly on overload instead of silently dropping audio. Queue and inference durations are instrumented. Regression test proves peak concurrency one.
2. Listening required both Silero p>=0.65 and an adaptive amplitude threshold. Soft phonemes/quiet speech could be vetoed despite speech evidence. Listening now uses p>=0.5 and continuing p>=0.3; playback remains stricter. Pre-roll grows from 448 ms to 800 ms. Short candidates require >=256 ms of supported speech. Endpoint silence decreases from 960 to 800 ms, still longer than a brief natural pause. This is a policy trade-off, not proof of improved accent accuracy.
3. ASR queued requests were not skipped before HTTP execution when cancelled. Add cancellation checks before shared-backend requests and bound admission to one running plus one queued job. Do not claim an executing shared-backend decode can be cancelled; stale results are suppressed.
4. Every utterance called speech health. Cache successful local-backend verification once per worker. Health warms the shared cached model; model, language and decoder unchanged.
5. Rendering the UI every 32 ms competes with inference on the browser main thread. Limit meter updates to 10 Hz.

## Audio / model verification

Silero's worklet resamples native microphone audio to 16 kHz, producing 512-sample frames. These samples are encoded once as mono PCM16 16 kHz WAV, with no second rate conversion. The new synthetic run observed 48 kHz native capture and ASR sample count exactly matching submitted segment count. Native driver drops are not exposed; metric is -1 (unknown), not a false zero.

Shared backend source/config: faster-whisper `base`, language explicitly `en`; existing `model.transcribe` defaults (no translation, prompt rewriting, or changed beam settings). Model is cached on backend state. No model switch. Backend HTTP route exposes no decoder/queue timing: `asr_upstream_roundtrip_ms` includes both, not claimed as pure decode time.

## Measurements

Current injected fixture, two turns (NOT physical microphone or Indian-accent acceptance):

| Stage | First | Warm |
|---|---:|---:|
| Estimated speech end to final transcript | 2071 ms | 1452 ms |
| Endpoint wait | 800 ms | 800 ms |
| Upload-to-final transcript | 1266 ms | 649 ms |
| Worker queue | 5 ms | 1 ms |
| Shared backend HTTP roundtrip | 1189 ms | 619 ms |
| Input samples | 70656 | 71680 |
| Queue peak | 1 | 1 |

No page errors; 35 seconds of processed capture stayed within ~23 ms of wall time at final measurement. Worker peak RSS 444 MiB. This sample does not prove long-session stability.

The previous physical samples of 1046–1365 ms are NOT directly comparable to this injected fixture. No valid before/after WER, human transcript latency percentiles, or actual audible-onset comparison has been established. Do not claim improvement from incomparable samples.

Host inspection found ~15 GiB RAM used, ~6.5 GiB compressed, ~16.1 GiB swap used and load average ~11.8, with several unrelated active workloads. This is a contention risk, not proof of a particular decode slowdown. No other processes were stopped.

## Diagnostics

Browser diagnostics: capture/context/native sample rates, first non-silent frame, frame sample totals, capture elapsed time, VAD decisions, queue high-water/wait/inference durations, segment length, endpoint wait, upload-to-transcript, AI acceptance/completion and playback scheduling. Adapter: total duration, pending depth, RSS. Worker: queue, upstream roundtrip, RSS. No new automatic transcript/audio logging. Playback scheduling is NOT acoustic first-audible time. TTS/model metrics already exist; no new live AI/voice benchmark was made during this recognition repair.

## Real microphone diagnostic run

A 60-second real Chromium microphone capture completed without browser errors, with one ASR segment (2080 ms) returning 3 words in 1272 ms after estimated speech end: 800 ms endpoint wait, 469 ms upload-to-transcript, 447 ms shared-backend roundtrip, 0 ms worker queue. VAD queue peak was one. Processed capture was 59936 ms over 60021 ms elapsed. The source was NOT labelled by the owner, and no ground-truth words were recorded: this is hardware-path evidence, NOT correct recognition acceptance and NOT proof that the segment was intentional speech rather than room audio. This run preceded correction of the misleading dropped_frames=0 field to -1 (unknown). Before/after WER and Indian-English acceptance remain unavailable.

## Owner physical acceptance — still required

Use existing preview http://127.0.0.1:5176/#/ai-workspace, close/reopen call after refresh. For recognition-only controlled collection run `REAL_MIC=1 TEST_SECONDS=120 node voice/recognition-check.mjs`. It uses real microphone permission, normal authenticated speech requests, no conversation generation, and stores counts/timings only. Cannot compute WER without explicit reference/hypothesis comparison; default privacy mode does not retain recognized words.

Fixed phrases to speak naturally at laptop distance, using the owner's Indian-English accent:
- Please check my calendar for tomorrow morning.
- I was thinking about the previous answer, [normal pause] but I would like you to explain it more simply.
- Can you tell me the difference between authentication and authorization without changing any settings?
- I need a moment to think, [normal pause] and then I will ask another question.
- Wait, let me finish what I was saying before you answer.

Collect quiet/noisy conditions, two and five consecutive turns, keyboard/fan sounds, deliberate speech during playback, missed words at both ends, false triggers and WER against exact spoken text (including genuine deviations from the script). Use the SAME recording with both pipelines for fair offline accuracy comparison only with owner authorization to retain it. Do not substitute generated speech for this acceptance. Keep immediate manual interruption available.
