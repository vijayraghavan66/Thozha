---
version: "1.0"
name: "Thozha"
description: "A quiet Android companion centered on honest Mac conversations, task status, and exact approvals."
colors:
  primary: "#9CB99A"
  background: "#0A0C0B"
  surface: "#121614"
  surface-raised: "#1A201D"
  border: "#263029"
  sage: "#9CB99A"
  text: "#EBEFEA"
  muted: "#98A49E"
  waiting: "#D6B77C"
  failed: "#D28A80"
typography:
  sans:
    fontFamily: "Roboto, system-ui, sans-serif"
    fontSize: "16px"
  display:
    fontFamily: "Roboto, system-ui, sans-serif"
    fontSize: "28px"
rounded:
  card: "18px"
  hero: "24px"
  control: "14px"
  capsule: "999px"
spacing:
  page: "20px"
  card: "16px"
  touch-target: "48px"
components:
  button: {}
  card: {}
  icon-navigation: {}
  connection: {}
  progress: {}
  approval: {}
---

# Thozha Android Design System

## Product direction

- **Audience:** A person using Thozha on Android alongside their Mac.
- **Primary job:** Ask Jarvis, see the Mac's real response and progress, and review exact-operation approvals.
- **Register:** A calm personal instrument, with conversation at its center and setup details available when needed.
- **Signature:** A three-stroke sage current mark reflects real activity. It stays still when idle and uses restrained spring motion only while a task is genuinely active; it never implies fabricated progress.
- **Product and personal identity:** Thozha (தோழா) is the product. The user may choose a separate personal assistant name in Settings; changing it does not change pairing, Mac identity, or the product label.
- **Locale:** English UI; dates and times use Android's locale-aware formatters.
- **Anti-references:** Public remote-control dashboard, fake progress percentages, and decorative analytics.

## Tokens and runtime ownership

Compose colors, typography, spacing, shapes, and motion are owned by `app/src/main/kotlin/com/jarvis/companion/ui/JarvisTheme.kt`. Frontmatter uses density-independent px equivalents (1 CSS px maps to 1 Android dp/sp at default font scale). Android XML colors and theme resources own only window chrome and non-Compose setup activities and mirror the same ink-and-sage palette. Navigation lives in `JarvisApp.kt`; screen behavior lives in the `ui/` package.

| Token | Value | Use |
|---|---|---|
| Background | `#0A0C0B` | Main surface |
| Surface | `#121614` | Conversation and cards |
| Raised surface | `#1A201D` | Selected navigation and secondary controls |
| Border | `#263029` | Quiet separation |
| Sage | `#9CB99A` | Active navigation and genuine task activity |
| Text | `#EBEFEA` | Primary copy |
| Muted | `#98A49E` | Supporting copy |
| Completed | `#9CB99A` | Confirmed outcomes |
| Waiting | `#D6B77C` | Pending approval or active caution |
| Failed | `#D28A80` | Failed or rejected outcomes |

## Typography and spacing

- Use Android's system sans-serif (Roboto on the supported device family); keep standard font scaling.
- Home request text uses 17 sp, assistant result text uses 16 sp, page titles use 26–30 sp, and state labels use 12–13 sp in sentence case.
- Keep long assistant results selectable and wrap them naturally. Do not clamp the answer in the conversation.
- Page inset: 20 dp. Conversation inset: 20 dp. Card inset: 16 dp. Important action targets: at least 48 dp.
- Cards use an 18–20 dp radius. Avoid shadows and ornamental borders where surface contrast is enough.

## Navigation and screens

- Four icon-only destinations: Home, Activity, Approvals, Connection. Settings remains an overlay. Every icon has an accessibility name and a 56 dp touch target.
- Home is the request and result surface. User messages align right; real Mac replies align left with a restrained Jarvis marker.
- Home's presence signal is driven by the latest task and composer state; it never implies progress the supervisor did not report.
- Activity presents real task states and summaries without inferred percentages.
- Approvals lists exact Mac operations; details retain the full readable operation, target, expiry, redacted arguments, and digest.
- Connection leads with paired/reachable state and last real sync. Raw relay endpoints and hardware diagnostics belong in advanced details, not the primary status.
- Settings exposes appearance, notifications, privacy, local-model controls, and Advanced setup.

## Interaction and motion

- The keyboard Send action and the send icon have the same behavior. Preserve the draft until it is accepted locally; disable duplicate sends while a request is in progress.
- A busy send keeps its button dimensions and displays an indeterminate spinner. No percentage is shown for work with unknown duration.
- Screen and navigation motion is brief and eased (140–180 ms). Reduced-motion settings disable it. Haptics are supplemental feedback.
- The presence signal uses three light strokes and animates only while a real task is active. Waiting approval, completion, failure, pause, and idle are static, distinct states.
- `adjustResize` keeps the composer available with the keyboard. Inputs and important controls remain reachable above system navigation.
- Errors stay attached to the task with a clear recovery step. A network timeout is described as unresolved because the Mac may still be working.
- A fresh/unpaired phone says setup is required. A configured relay URL or online phone never implies that the Mac is paired or reachable.

## Approval presentation

Show an approval only when its operation, target, expiry, exact digest, and redacted arguments are present. Use explicit “Approve once” and “Reject” actions. An accepted decision is not described as a successful execution until the Mac reports its result.

## Restraint

Task state, the request, and the genuine assistant result carry the hierarchy. Keep the shell quiet, preserve accessible text labels, and do not use color alone to communicate state.
