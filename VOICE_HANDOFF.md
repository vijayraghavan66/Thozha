# Isolated Web voice work boundary

Branch `sahaa/web-conversational-voice`, based on `copilot/web@16c03db1`.

Only this worktree's `web/` is modified. No active OpenJarvis, LAN gateway,
Android, certificates, pairing or shared state configuration is changed.
The optional development-only speech adapter listens through this worktree's
loopback Vite server at 127.0.0.1:5176, not through the LAN gateway.
It validates the existing Web session and CSRF before local audio processing.
The existing authenticated Web conversation API owns history and model execution.
No direct model generation or automatic action approval is added.

Integration requires review by the Web and backend owners. No merge, push or
service restart is performed. The optional adapter is intentionally not a
production deployment; backend owner should mount equivalent speech handlers
behind the established Web authentication dependency for production.
