# Latest authoritative status

Read FINAL_HANDOFF.md and INDEPENDENT_BETA1_QA.md before older README/VALIDATION
notes. Those files describe historical implementations and evidence, not current
release acceptance. Current voice is a blocked integration candidate, not complete.

Final-pass automated results: 69 JavaScript tests; 4 Python tests; build/lint pass.
Authenticated headless preview health: ready Kokoro, semantic turn ready, no partial
ASR. Physical browser microphone startup failed with `Not supported`; no current
natural spoken barge-in result, false-interruption rate, WER or audible percentiles.
No owner prompts or synthetic microphone events are counted as physical acceptance.

No shared backend, gateway, Android, certificates or identity changes. Production
voice and durable Luna delta/history contracts belong to Codex. See handoff matrix.
