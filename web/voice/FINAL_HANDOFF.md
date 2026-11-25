# Thozha Live — independent candidate, NOT release acceptance

## Integration ownership and status

Codex owns backend, deployment and release. This branch changes only `web/`.
No shared backend/service restart, Android change, pairing mutation or LAN-policy
change was made in this pass. Do not merge this branch blindly: review the diff
from `a40fc0b7`, then integrate only the voice paths/dependency changes. Keep the
separate completed Web quality branch independent.

Voice is **BLOCKED for release**, not Gemini Live equivalent. The authenticated
preview on http://127.0.0.1:5176 loads local Kokoro and semantic endpointing.
The static production build does not implement voice handlers or serve model
assets: `voice/plugin.ts` is an opt-in development adapter only.

## Provenance / selected architecture

Research checkouts under `../thozha-voice-research` (relative to worktree parent):

| Project | Inspected revision | Decision |
| --- | --- | --- |
| kyutai-labs/unmute | e348e56 | MIT orchestration; actual README requires x86/CUDA, not supported as a drop-in Mac stack. Its audio/LLM overlap needs genuinely incremental Luna text. |
| livekit/agents | d659580 | Apache-2.0 code. Study reversible interruption/lifecycle semantics; no source copied. Model license limits model reuse to LiveKit; adaptive interruption inference can be remote. No external audio service enabled. |
| pipecat-ai/pipecat | e2c83d4 | Reuse actual Smart Turn feature extraction and local v3.2 int8 model contract, BSD-2-Clause with retained Apache-derived notices. See vendor/pipecat/NOTICE.md. |
| kyutai-labs/moshi | e6a55d2 | MIT/Apache code, separate model licenses. MLX exists, but native speech-to-speech introduces another conversational brain. No Luna replacement or large weight download. |
| pipecat-ai/smart-turn | 4e12f2b | BSD-2-Clause local semantic completion; actual local inference verified. |

Browser MicVAD/Silero -> ordered recurrent inference -> Pipecat semantic endpoint
-> existing local whole-turn Whisper -> durable authenticated conversation POST
and owner polling -> bounded sentence synthesis -> resumable browser playback.
Local peer-connection loop provides the browser RTC render path; it is not proof
that hardware echo cancellation is effective. No STUN/TURN or remote peer.

## Changes included

- Serialized recurrent VAD inference, bounded queue with visible fail-closed
  overload, 800 ms pre-roll, quiet speech not vetoed by an extra listening RMS gate.
- Separate playback speech confidence and residual-speech probe after reversible
  attenuation; no full-ASR wait before pausing playback. ASR confirms the completed
  new turn before replacing an answer. Empty/current-phrase echo resumes audio
  at the retained sample offset, not from the beginning.
- Prevent chunk-boundary playback and request-completion races during probes.
- Retain confirmed speech across interruption transition; invalidate stale semantic
  decisions on mute/manual interrupt; serialize interruption confirmation.
- Local semantic endpoint at 320 ms candidate silence, retry when incomplete,
  conservative 1200 ms fallback if unavailable. Bounded turn contract preserved.
- End/mute stop or invalidate capture, confirmation and queued playback. Pending
  durable request identity remains for explicit recovery, never auto-starting mic.
- First phrase bounded near 120 characters; one-sentence lookahead overlaps neural
  synthesis/playback. Models reused; no paid/system/cloud speech fallback.
- Stage diagnostics and resource counters. Driver frame loss remains UNKNOWN (-1),
  not zero. Audio is not stored by call processing.
- Test harness refuses to label a failed microphone startup as acoustic success.

## Independently executed validation in final pass

- 69 JS tests, 15 files: PASS; production build and ESLint: PASS.
- 2 semantic preprocessing and 2 ASR cancellation/admission Python tests: PASS.
- Headless authenticated preview/session/speech health: PASS, no page errors;
  `ready=true`, `semantic_turn=true`, `partial_transcription=false`.
- Final recognition-only injected-audio browser check: two turns, no page errors,
  queue peak one, zero retained frames after End. Playback-reference RTC reported
  fallback (0) in this headless run: no claim of verified RTC echo cancellation.
- Physical microphone/speaker attempt: BLOCKED. Startup returned `Not supported`
  before live capture. Previous harness incorrectly continued trials after ended
  startup; now fails explicitly. No privacy settings modified or repeated owner
  speech requests. No current human barge-in denominator exists.
- Runtime policy AST reproduction: FAIL, approval-free mutation classification.
  Run `python -B voice/audit_policy.py /absolute/runtime/tasks/supervisor.py`.
  It extracts only the classifier and never executes the classified commands.

## Measurements and evidence classes

Historical local artifacts in ignored `voice/results/` are NOT final-code physical
acceptance. They may be overwritten by harnesses; summary below preserves relevant
numbers without private transcript/audio. Do not compare unlike runs as speedup.

| Evidence | Result | Qualification |
| --- | --- | --- |
| Smart Turn generated neural clips | 33–36 ms inference | Synthetic, no evidence of Indian-English endpoint accuracy |
| Alternating warm first-phrase TTS | 308–344 ms vs 617 ms full sentence | Real local inference; not acoustic first-audible latency |
| Earlier acoustic stimulus run | endpoint 800 ms; ASR worker queue 1331 ms; ASR HTTP/decode 849 ms; AI acceptance 3021 ms from speech end; completed answer 18187 ms; scheduled playback 18371 ms | Physical speaker/mic, synthetic stimulus, not owner speech; threshold onset not validated acoustic correlation |
| Latest saved A/B Kokoro | 620–741 ms warm, 16.2 s cold; peak process 338 MiB | Generated samples; loading/host contention differs from earlier runs |
| Latest saved A/B Qwen Ryan | 4.1–12.5 s first chunk; peak process 718 MiB | Same process follows Kokoro; cumulative peak, not isolated model memory. Current HTTP returns complete phrase, not first Qwen chunk |
| Host inspected now | ~16.24 GB swap used | No other workloads stopped; no GPU utilization measurement available |

Kokoro remains default for predictable throughput. Qwen Ryan is optional, not a
quality winner. Existing A/B samples require human listening; warmth, Indian-name
pronunciation and natural breathing have NOT been independently scored.

## Required backend work — Codex only

See CODEX_HANDOFF.md. The installed CodexSDKEngine.stream awaits generate before
its single yield. Source inspection at f4a49df1 does not prove a long-running
process's loaded version. The experimental Web SSE path at 86e09d3a cancels and
finishes its response on subscriber disconnect. Keep durable submission enabled.

Required: disconnect-independent generation, durable public-answer deltas with
owner-authenticated replay, explicit authorized cancellation and same-key binding.
Expose a capability only when tested end to end. Do not emit reasoning/tool data.

Also needed: authenticated playback-progress/interruption metadata that records
which assistant prefix was actually heard without deleting the full audit answer.
The client cannot safely rewrite existing server history to fake truncation.
Until then, interruption stops/supersedes output but Luna still sees the full saved
answer. This is a documented contextual limitation, not fulfilled truncation.

## PASS / FAIL / BLOCKED

| Acceptance | Status |
| --- | --- |
| Existing authenticated API/history and durable IDs retained | PASS source/regressions; final full recovery browser run not repeated |
| Local-only speech / no paid fallback | PASS implementation; no packet-capture claim |
| Build, lint, unit/component regressions | PASS |
| Actual semantic model and authenticated preview health | PASS |
| Twenty natural turns / Indian-English WER | BLOCKED, no labelled consented human corpus |
| Genuine speaker-based human interruption / reaction percentiles | BLOCKED, startup failure; zero labelled current attempts |
| False-interruption frequency on current physical build | BLOCKED, no valid exposure duration |
| First-audible end-to-end latency percentiles | BLOCKED, no valid acoustic onset correlation |
| Durable incremental Luna text | BLOCKED backend contract |
| Interrupted-history heard-prefix handling | BLOCKED backend contract |
| Production speech deployment | BLOCKED backend-owner integration |
| Supervisor mutation classification | FAIL, see INDEPENDENT_BETA1_QA.md |

## Codex integration sequence

1. Review this branch and security reproducer; do not import local results/weights.
2. Integrate dependency lock plus src/voice and attributed preprocessing as a unit.
3. Implement authenticated production speech handlers, asset serving and worker
   lifecycle in a Codex-owned branch; preserve exact session/CSRF checks and bounds.
4. Complete durable public-delta/history-progress contracts with replay/security tests.
5. Run fresh browser microphone startup on the installed browser without modifying
   TCC programmatically. Capture exact failure stage if unsupported.
6. Use consented labelled human audio for WER and prerecorded double-talk fixtures;
   never treat neural synthesized clips as Indian-English human acceptance.
7. One final owner listening session after independent failures are resolved.
8. Release voice only after actual spoken barge-in, noise/echo and twenty-turn gates
   pass. No claim of 1–3 second response is supported by the current measurements.
