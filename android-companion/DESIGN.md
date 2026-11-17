# Jarvis Companion Design System

## Product direction

- **Audience:** The owner of a Mac running Jarvis.
- **Primary job:** Send an assistant request, read the Mac's real result, and review exact-operation approvals.
- **Register:** A quiet instrument panel with a conversation at its center.
- **Signature:** A compact three-stroke signal beside the Jarvis title reflects the latest real task state; each Mac response starts with a small cyan “JARVIS” label so assistant output reads separately without extra chat chrome.
- **Locale:** English UI; dates and times use Android's locale-aware formatters.
- **Anti-references:** Public remote-control dashboard, fake progress percentages, and decorative analytics.

## Tokens and runtime ownership

Android color resources are the runtime color source in `app/src/main/res/values/colors.xml` and `values-night/colors_jarvis.xml`. Theme and shape defaults live in `values/styles.xml` and `values/themes.xml`. Screen layout and component behavior live in `MainActivity.kt`.

| Token | Value | Use |
|---|---|---|
| Background | `#091117` | Main surface |
| Surface | `#0D1921` | Conversation and cards |
| Raised surface | `#13232D` | Selected navigation and secondary controls |
| Border | `#29404A` | Quiet separation |
| Cyan | `#51DCE0` | Active navigation, task activity, assistant marker |
| Text | `#EAF3F4` | Primary copy |
| Muted | `#A0B2B9` | Supporting copy |
| Success | `#8DE3B7` | Completed outcomes |
| Warning | `#FFC06F` | Pending approval or active caution |
| Danger | `#FF8082` | Failed or rejected outcomes |

## Typography and spacing

- Use Android's system sans-serif (Roboto on the supported device family); keep standard font scaling.
- Home request text uses 17 sp, assistant result text uses 16 sp, page titles use 26–30 sp, and state labels use 13 sp.
- Keep long assistant results selectable and wrap them naturally. Do not clamp the answer in the conversation.
- Page inset: 24 dp. Conversation inset: 20 dp. Card inset: 16 dp. Important action targets: at least 48 dp.
- Cards use an 18–20 dp radius. Avoid shadows and ornamental borders where surface contrast is enough.

## Navigation and screens

- Three icon-only destinations: Home, Activity, Settings. Every icon has an accessibility name and a 56 dp touch target.
- Home is the request and result surface. User messages align right; real Mac replies align left in a restrained surface card.
- Home's presence signal is driven by the latest task and composer state; it never implies progress the supervisor did not report.
- Activity lists supervisor task records and approvals from the paired Mac. Details retain full readable operation, target, expiry, redacted arguments, and digest.
- Settings exposes connection status, notification permission, appearance, privacy, and Advanced setup.

## Interaction and motion

- The keyboard Send action and the send icon have the same behavior. Preserve the draft until it is accepted locally; disable duplicate sends while a request is in progress.
- A busy send keeps its button dimensions and displays an indeterminate spinner. No percentage is shown for work with unknown duration.
- Screen and navigation motion is brief and eased (140–180 ms). Reduced-motion settings disable it. Haptics are supplemental feedback.
- The presence signal uses three light strokes and animates only while listening or while a real task is active. Waiting approval, completion, failure, pause, and idle are static, distinct states.
- `adjustResize` keeps the composer available with the keyboard. Inputs and important controls remain reachable above system navigation.
- Errors stay attached to the task with a clear recovery step. A network timeout is described as unresolved because the Mac may still be working.

## Approval presentation

Show an approval only when its operation, target, expiry, exact digest, and redacted arguments are present. Use explicit “Approve once” and “Reject” actions. An accepted decision is not described as a successful execution until the Mac reports its result.

## Restraint

Task state, the request, and the genuine assistant result carry the hierarchy. Keep the shell quiet, preserve accessible text labels, and do not use color alone to communicate state.
