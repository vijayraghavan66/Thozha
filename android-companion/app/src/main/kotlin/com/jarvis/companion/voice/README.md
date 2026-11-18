# Voice controller integration contract

`JarvisVoiceController` is a UI-independent Android speech input/output component. It does not submit tasks, inspect approvals, or execute commands. The lead-owned composer should pass the final recognized text into the existing composer draft/submission path.

## Push-to-talk lifecycle

1. Create one controller for the screen or activity lifecycle and implement `JarvisVoiceController.Listener`.
2. Before recording, check `JarvisVoiceController.hasMicrophonePermission(context)`. If false, launch Android's runtime `RECORD_AUDIO` permission request. Do not create the controller as a substitute for that permission flow.
3. After a grant, call `startPushToTalk()` on press. Call `releasePushToTalk()` on pointer/key release; `cancel()` on gesture cancellation, app interruption, or explicit stop.
4. Put partial text in a temporary preview only. On `onFinalText`, update the normal composer draft; never auto-submit from the recognition callback.
5. Map controller errors to concise UI states. `PERMISSION_REQUIRED` should re-open the system permission flow; `RECOGNIZER_UNAVAILABLE` / `NETWORK_OR_SERVICE` should offer typed input; no-match and timeout should return to idle.
6. Call `close()` on disposal. It cancels/destroys `SpeechRecognizer`, stops/shuts down TTS, and releases audio focus.

## Playback

Call `speak(answer)` for an answer the user chose to hear; call `stopSpeaking()` when the user interrupts. Text-to-speech waits for engine initialization and flushes prior utterances. The system TTS engine and its installed voice determine audio quality and supported languages.

## Privacy and runtime notes

- Android runtime microphone permission is required and is declared in the manifest.
- Recognition prefers Android's on-device recognizer when the OS reports one. On devices without one it falls back to the configured system recognition service. `EXTRA_PREFER_OFFLINE` is advisory; the system provider may still use network services. The UI should disclose that provider behavior and offer typed input.
- Audio samples and recognized text are never written to app logs by this component. The final transcript is returned only to the caller.
- All methods must be called on the main thread. The Android recognition service and audio-focus APIs are not JVM-testable without a device/runtime; exercise permission denial, recognition, interruption, TTS and lifecycle on a real Android device before claiming device verification.
- Hold-to-talk must be usable by touch and accessibility. Expose a semantic label/state and an explicit cancel path; do not rely on color or animation to communicate recording.
