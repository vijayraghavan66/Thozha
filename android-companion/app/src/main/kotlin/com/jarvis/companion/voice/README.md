# Voice controller integration contract

`JarvisVoiceController` is a UI-independent Android speech input/output component. It does not submit tasks, inspect approvals, or execute commands. The lead-owned composer should pass the final recognized text into the existing composer draft/submission path.

## Push-to-talk lifecycle

1. Create one controller for the screen or activity lifecycle and implement `JarvisVoiceController.Listener`.
2. Before recording, check `JarvisVoiceController.hasMicrophonePermission(context)`. If false, launch Android's runtime `RECORD_AUDIO` permission request. Do not create the controller as a substitute for that permission flow.
3. After a grant, call `startPushToTalk()` on press. Call `releasePushToTalk()` on pointer/key release; `cancel()` on gesture cancellation, app interruption, or explicit stop.
4. Put partial text in a temporary preview only. On `onFinalText`, update the normal composer draft; never auto-submit from the recognition callback.
5. Map controller errors to concise UI states. `PERMISSION_REQUIRED` should re-open the system permission flow; `RECOGNIZER_UNAVAILABLE` / `NETWORK_OR_SERVICE` should offer typed input; no-match and timeout should return to idle.
6. Call `close()` on disposal. It cancels/destroys `SpeechRecognizer`, stops/shuts down TTS, and releases audio focus.

`startVoiceTurn()` is an optional single-turn mode that lets Android's speech endpointer close the utterance at a pause. It still returns a transcript callback only; the application must show the editable draft and require the existing Send action. It is not a continuously open microphone or a full-duplex call.

## Playback

Call `speak(answer)` for an answer the user chose to hear; call `stopSpeaking()` when the user interrupts. Text-to-speech waits for engine initialization and flushes prior utterances. The controller selects the highest-quality installed non-network voice for the requested language and excludes voices marked as not installed; it does not silently select a voice that requires network or download. It reports a `VoiceOutputProfile` (voice name, locale, quality, latency and offline status) to the host UI. If there is no suitable offline voice, it reports speech unavailable instead of silently selecting an online voice. This is a local best-available policy, not a verified naturalness claim.

## Privacy and runtime notes

- Android runtime microphone permission is required and is declared in the manifest.
- Recognition prefers Android's on-device recognizer when the OS reports one. On devices without one it falls back to the configured system recognition service. `EXTRA_PREFER_OFFLINE` is advisory; the system provider may still use network services. The controller reports `ON_DEVICE` versus `SYSTEM_FALLBACK` to the host UI so it can disclose provider behavior and offer typed input. A failure from the on-device recognizer does not silently switch providers.
- Audio samples and recognized text are never written to app logs by this component. The final transcript is returned only to the caller.
- Speech callbacks are bound to a monotonically changing session generation. Cancellation/restart discards late partial/final callbacks; TTS callbacks are bound to the active utterance ID, so a stopped utterance cannot change the next turn's state.
- All methods must be called on the main thread. The Android recognition service and audio-focus APIs are not JVM-testable without a device/runtime; exercise permission denial, automatic endpointing, interruption, TTS and lifecycle on a real Android device before claiming device verification.
- Hold-to-talk must be usable by touch and accessibility. Expose a semantic label/state and an explicit cancel path; do not rely on color or animation to communicate recording.

## Voice limits

This controller provides Android ASR, platform endpointing for an optional single turn, and installed local TTS. Android documents `EXTRA_PREFER_OFFLINE` as advisory, so `SYSTEM_FALLBACK` may involve a network speech service; this must be disclosed by the UI. It does not implement streaming speech-to-speech or persistent full-duplex conversation; that requires a separate authenticated low-latency audio transport and a selected voice model/provider. No audio-stream provider is configured here.

API references: [SpeechRecognizer](https://developer.android.com/reference/android/speech/SpeechRecognizer), [RecognizerIntent](https://developer.android.com/reference/android/speech/RecognizerIntent), [Voice](https://developer.android.com/reference/android/speech/tts/Voice), [TextToSpeech.Engine](https://developer.android.com/reference/android/speech/tts/TextToSpeech.Engine).
