# Focused Web quality pass

Scope: isolated `sahaa/web-quality-pass`, based on `copilot/web` at `16c03db1`.
No voice-worktree, speech-service, backend, LAN, Android or authentication changes.

## Changes
- Settle the silver WebGL identity after its initial 1.2-second idle period. Meaningful state changes still wake rendering; reduced-motion, visibility and context-recovery behavior remain supported.
- Remove perpetual idle SVG breathing and staggered/perspective welcome entrances. Retain a short 220 ms entrance with the existing silver/monochrome styling.
- Increase mobile composer controls to 44 px and remove redundant toolbar padding so the composer remains within its existing compact-height budget.
- Make response tables focusable, labelled horizontal-scroll regions for keyboard users.

## Validation
One final sequential regression run: production build, TypeScript, ESLint, 31 unit tests and all 33 workspace browser tests passed.
Browser tests ran headlessly, with one worker, isolated fixture storage and an inert model. They did not call the shared supervisor or speech services.
Coverage includes cookie sign-in/out, chat/history, interrupted request recovery, cancellation, navigation, mobile layout, 200% zoom, attachments, accessibility scans and reduced motion.

Measured Chromium requestAnimationFrame intervals during the final run:
- Recorded walkthrough: p95 16.80 ms; 8 of 1,668 intervals exceeded 25 ms.
- Unrecorded navigation: p95 16.80 ms; 2 of 143 intervals exceeded 25 ms.
- Launch outliers remain: reduced-motion launch included one 216.6 ms interval. These short, headless samples do not prove sustained 60 FPS or eliminate startup jank.

Verified production output: `web/dist/`. No persistent preview was launched or shared service restarted.
Raw logs remain outside the repository at `/tmp/thozha-web-quality-logs/`.

## Remaining limits
This branch deliberately excludes unfinished voice changes. Browser coverage uses isolated backend fixtures, not live-device or live-model acceptance. Existing experimental sections and backend availability are not made production-ready by this presentation-only pass.
