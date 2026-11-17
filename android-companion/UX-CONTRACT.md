# Jarvis Companion UX Contract

## Product context

- **Audience:** Owner pairing one Android phone with one Mac Jarvis instance.
- **Primary jobs:** Send a free-text assistant request, see the Mac's genuine result, track tasks, and make informed decisions on exact-operation approvals.
- **Locale:** English UI with Android system fonts, date formatting, and scalable text.
- **Accessibility:** Native Android controls, accessible names for icon-only navigation, visible focus, touch targets of at least 48 dp for primary actions, and reduced-motion support.

## Canonical UI map

| Capability | Canonical owner | Source of truth | Verification |
|---|---|---|---|
| Main navigation | `MainActivity` three-item bottom navigation | `DESIGN.md` | Build and phone review |
| Free-text request and reply | Home conversation in `MainActivity`; `LinkRuntime` local record | This contract and the authenticated Mac task API | Android build, Mac tests, real-device task |
| Task status/result | Activity task list and task detail | OpenJarvis supervisor result | API result plus phone display |
| Link approval review | Activity/detail in `MainActivity` | Encrypted Link approval event and Mac supervisor | Exact digest and device decision test |
| Pairing invitation scan | Google Play services Code Scanner | Signed QR contract | Phone scan and Mac confirmation |
| Device credential | Android Keystore and encrypted `SecureStore` | Pairing API | Restart and authenticated request |

## Home task flow

1. The user submits a non-empty request from the Home composer by tapping Send or the keyboard Send action.
2. The app creates a random idempotency key, saves the message locally as `RUNNING`, and sends it with the existing paired-device bearer token to the Mac's authenticated `/v1/jarvis/link/tasks` route.
3. The Mac authenticates the paired device, binds the request to the server-approved `assistant` scope, invokes the configured Codex SDK agent, and persists the task/result digest in the supervisor.
4. Read-only observations can run directly. Every state-changing Mac MCP call creates an exact supervisor approval and pauses the task; the privileged executor runs the stored operation only after the user approves it.
5. After the approved operation, the Mac continues the task using the original request supplied by the paired phone and verifies that its digest matches the stored task digest.
6. The app displays the returned task state and actual summary. The assistant result is selectable and aligned separately from the user's message.
7. Activity can cancel an active task using its phone-generated idempotency key. The Mac verifies paired-device ownership, cancels the task atomically, and expires any approval that has not been consumed. If the one-time Mac dispatch was already claimed, the response warns that the action may have started; cancellation stops only the remaining continuation.
8. If the response is interrupted, the app checks status using the same idempotency key every five seconds while active (15 seconds after a connection error). It refreshes pending approvals when status becomes `WAITING_APPROVAL` and never resubmits work during recovery.

Home tasks use a server-bound assistant scope. Codex's shell and file-change requests stay denied; desktop mutations pass through the exact-operation approval record. The interface must not imply that a privileged operation was approved or executed before the Mac confirms it.

## Async, failure, and recovery behavior

| Situation | UI behavior |
|---|---|
| Sending | Preserve button size, show an indeterminate spinner, and prevent duplicate submission. |
| Completed | Show the Mac task state and genuine result; do not replace an empty or failed result with a canned answer. |
| Mac returns `RUNNING` / `VERIFYING` | Keep the task active and show the status. Resolve it by querying the same idempotency key. |
| Timeout / lost connection | Say the result is unconfirmed; keep the task history and check status before any resend. |
| Task not found | Explain that the Mac did not record it and tell the user to check connection before retrying. |
| 401 / expired credential | Keep existing pairing data; explain that authentication failed and leave recovery to the user. |
| 403 | Explain that the Mac blocked a request outside the enabled task scope. |
| Mac/Codex unavailable | Keep the request visible and state that the Mac runner is unavailable. |
| Approval pending | Show exact operation details and separate Approve/Reject actions. Never infer consent from a label or notification tap preview. |
| Cancel requested | Confirm the request, show “Cancelling on your Mac…”, and display the Mac's resulting state. Explain if a previously claimed action may already have started. |

Do not queue a privileged approval decision offline. After interruption, refresh the authoritative approval state before showing it as pending or accepted.

## Navigation, keyboard, and motion

- Home, Activity, and Settings are peer destinations represented by icon-only controls with accessibility labels.
- The input uses `IME_ACTION_SEND`; tapping the send control and keyboard action perform the same operation.
- The window uses `adjustResize`, and the composer remains above the virtual keyboard and bottom navigation.
- Animations are short and eased. Respect the system reduced-motion setting. Animation never replaces text state or action feedback.

## Connection and trust boundaries

- The raw OpenJarvis API stays loopback-only. USB/ADB reverse is the fallback path.
- The optional Wi-Fi gateway is a separate allowlisted HTTPS service; it proxies only paired-device task, approval, and registration routes to the loopback backend. It binds only to one configured private interface address and only advertises on a configured SSID.
- The Android client pins the gateway's TLS certificate through a nonce-bound signature from the already-paired Mac key over USB. It discovers the pinned `.local` service automatically and retries discovery on later requests. It never sends the bearer token before the pinned TLS handshake succeeds.
- Wi-Fi gateway startup is disabled unless an explicit enable flag, Wi-Fi interface, private bind IP, and SSID allowlist are configured. It must stay disabled until the device/network policy permits it; do not bind the raw Mac API to a LAN interface.
- Android 17's `ACCESS_LOCAL_NETWORK` runtime permission is requested from Settings > Mac connection when the user asks to enable Wi-Fi access. If denied, the paired USB path remains available.
- Tailscale, public tunnels, and Funnel are not part of this client configuration.
- The permanent Mac API key never enters the APK. Pairing tokens remain encrypted in Android app storage; signing keys stay in Android Keystore.

## Approval and notification boundaries

- Approval records show the exact operation, target, canonical argument summary, expiry, and full operation digest before a decision.
- New direct companion tasks are bound to the authenticated paired-device identity. A second enrolled device cannot read or cancel another device's new task.
- The Android app sends only the explicit user's Approve or Reject decision. The Mac independently verifies task ID, digest, expiry, replay status, and the privileged operation.
- After approval, the Mac consumes the stored operation once and continues the associated assistant task. A service restart pauses tasks and leaves approval decisions explicit; it never replays them silently.
- Cancelling an active Link task changes its durable task state to `CANCELLED` and revokes pending or approved-but-unconsumed approvals in the same transaction. A claimed MCP dispatch is recorded and cannot be claimed again.
- A notification is only a pointer to a request. It does not approve it, and notification text must not disclose secrets.
- The FCM backend sends data-only approval IDs. The app fetches the approval through its authenticated Mac connection before posting the high-importance, vibrating notification.
- FCM send/receipt is not verified until Firebase ADC or service-account credentials are available and the phone receives a real message. No push-delivery claim should be made before that test.

## Assistant message rendering

- Assistant text is treated as plain, untrusted text. The companion renders only balanced bold (`**text**`) and inline code (`` `text` ``) spans.
- It does not parse HTML, links, images, or other executable or remote markup. Unmatched markers stay visible.
- JSON newlines remain newlines, and a literal `%n` remains literal text; the app does not run assistant content through string-format interpolation.

## Verification

- Build: `./gradlew assembleDebug`.
- Protocol check: `./gradlew verifyLinkProtocol`.
- Mac regression tests must cover loopback/auth/scope/idempotency, tool-boundary denial, restart behavior, and the existing approval suite.
- Phone acceptance is complete only after the Vivo sends a request over the existing paired route and visibly renders the real Codex result. Device connectivity and FCM status must be recorded separately.
