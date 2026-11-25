# Voice 2 physical acceptance protocol

Do not report this checklist as passed without observations. Silero distinguishes speech from noise, not speaker identity. Browser AEC plus ASR echo rejection is defense in depth, not guaranteed acoustic separation.

Run only one call at a time; stop synthesis benchmarks during latency acceptance. Keep browser speaker output and Mac microphone on known physical devices. Do not use fake microphone flags for acceptance.

1. Open http://127.0.0.1:5176/#/ai-workspace and Voice. Record device names, output volume, distance and room noise.
2. Speak twenty distinct turns. Include 300, 600 and 800 ms thinking pauses. Annotate intended speech ends and response audible onsets using physical microphone recording or instrumented acoustic correlation. Scheduled audio is not proof.
3. During five complete answers type normally; during five create ordinary room/fan noise; during five cough briefly. Count false interruptions per answer and per minute. Do not induce dangerously loud sound.
4. During ten answers deliberately say a distinct phrase over playback. Count stopped outputs, measure onset-to-acoustic-stop, confirm full interruption phrase and continuation become one next turn. Repeat with words also used by the assistant to reveal conservative echo rejection false negatives.
5. Replay no external voice when testing self-echo. Assistant audio alone must not create user submissions.
6. Alternate follow-ups to verify context. Compare durable user/assistant IDs against accepted turns, not request counts alone.
7. Mute mid-capture, mid-confirmation and mid-playback; End during model warmup and recovery. Verify stopped tracks and no later output. Refresh never starts a microphone automatically.
8. Disconnect/reconnect after durable acceptance; compare request IDs and history; no new key for the same uncertain request.
9. Listen to Bella and Ryan with identical normal, empathetic and enthusiastic text. Score intonation, phrasing, pronunciation, continuity and artificial artifacts. Do not infer emotion/breath quality from inference timings.

Metrics: false interruption count / playback minutes; successful deliberate interruptions / attempts; speech-end→first-acoustic-output p50/p90/p95; speech-onset→acoustic-stop p50/p95; final transcript and model completion separately. Twenty samples alone provide only preliminary tail estimates.

Owner participation is required for natural speech, keyboard/cough labels and subjective voice preference. Acoustic synthesized playback is a useful hardware regression but is not natural user acceptance.
