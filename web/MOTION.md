# Workspace motion

The graphite layout, API clients and authorization boundaries are unchanged.
Motion describes real UI state; it never simulates model tokens, task progress,
device presence, approvals or signed pairing invitations.

## Shared vocabulary

[`motion.css`](./src/motion.css) owns the CSS tokens and choreography.
[`motion.ts`](./src/components/motion.ts) provides matching Web Animation
timings, the scene-arrival hook, motion eligibility and task identity mapping.
Keep the CSS and JavaScript timings aligned when changing this vocabulary.

| Purpose                                          | Duration |
| ------------------------------------------------ | -------- |
| Press feedback                                   | 110 ms   |
| Controls, input borders and composer resizing    | 180 ms   |
| Panels, navigation, dialogs and message arrivals | 280 ms   |
| Page/conversation arrival                        | 320 ms   |
| Welcome reveal and completion sweep              | 560 ms   |
| Dismissal                                        | 160 ms   |

The common easing is `cubic-bezier(.22,.8,.24,1)`. Panel resizing uses a
restrained, non-overshooting spring-like curve. No sound or haptic effects exist.

### Navigation and surfaces

- The existing sidebar, contextual history and active-item surface retain their
  identity. Labels fade as the sidebar resizes; preference persistence is unchanged.
- Supported browsers use a named native View Transition for the main workspace,
  with direction derived from navigation order. Navigation remains live outside
  the snapshot. A newer navigation skips the previous transition.
- Browsers without View Transitions update the route immediately and retain the
  content-arrival effects. The chat subtree and unsent drafts are not remounted.
- Anchored menus use opacity and transform rather than animated clipping.
  `inert`, `aria-hidden`, Escape and existing focus management remain authoritative.
- Dialog close buttons, Escape and backdrop dismissal animate before unmount.
  Dialogs become inert during dismissal. Reduced-motion cancellation still
  completes dismissal; it cannot strand an inert top-layer dialog.
  Parent-driven success/cancel unmounts remain immediate.
- Buttons compress slightly on press, not on generic hover. Input focus and
  panel focus-within use quiet border changes.

### Welcome, conversation and tasks

- Two bounded monochrome contours and the small SVG create the welcome
  atmosphere. There is no new full-screen shader, particle layer or animation library.
- Heading, subtitle, composer and starter suggestions arrive in sequence.
  The composer moves without fading its readable input or command palette.
- Only newly added messages receive message entrance motion. Saved conversation
  switching animates its container; completed Markdown fades without translating
  code-copy controls.
- Actual incremental deltas retain the existing requestAnimationFrame batching.
  Buffered results appear whole. Following live deltas does not repeatedly restart
  smooth scrolling. Text remains ordinary selectable DOM.
- Textarea resizing is interruptible and bounded at 160 px. Sidebar resizing and
  this small textarea are deliberate layout-animation exceptions; most effects
  use transform and opacity.
- Task state chips and newly received event rows transition from server data.
  Initial replay is not presented as new activity. Queued is marked only when a
  queue event exists; no authorization milestone or percentage is inferred.
  Milestone cards adapt to the detail panel's width, including split-pane tablets.

### Living identity

| Actual state                                        | Presentation                |
| --------------------------------------------------- | --------------------------- |
| Idle                                                | Low-intensity SVG breathing |
| Chat request pending                                | Silver contours flow        |
| Supervisor CREATED / PLANNING / RUNNING / VERIFYING | Controlled ring rotation    |
| WAITING_APPROVAL / pending approval                 | Gentle ring emphasis        |
| Completed after a state change                      | One short sweep             |
| Failed                                              | Muted broken-ring treatment |
| Cancelled                                           | Quiet, dimmed mark          |

Historical completed messages do not replay a completion sweep. Pairing status
continues to mean pairing, not online connectivity. Existing invitation countdown,
fingerprint review, cancellation and revocation behavior is unchanged.

## Accessibility and resource use

System reduced motion and **Settings -> Visual preferences** disable decorative
animations and transitions. Changing the preference also cancels active Web
Animations and page snapshots. Hidden documents pause CSS ambient effects;
new JavaScript arrival effects are not started while hidden. Hidden retained chat
content does not run ambient animation.

Existing keyboard navigation, focus restoration, modal locking and independent
task/history scrolling remain in place. No per-token shell-state updates were
added. Prefer bounded elements and avoid continuous blur, filters or layout reads.

## Verification

The motion change was checked against the real owner-authenticated application
on port 5174 and isolated API/model fixtures. These are distinct forms of evidence.

- Live walkthroughs: 1440x900, 1280x800, 930x581, 768x1024 and 390x844.
  Each opens saved history and task details, Device Hub, the genuine private
  gateway-unavailable pairing dialog, current approvals, profile and notifications.
- The owner-approved prompt **what is java** returned a real answer over HTTP 200
  `text/event-stream` and remained after navigation/reload. No artificial token
  playback was introduced.
- Live approvals were empty. Exact-operation review and explicit decisions were
  recorded against the isolated control fixture, not a production Mac operation.
- Before/after clips use the published frontend at `68be4314` and the new
  implementation against the same approved backend. The baseline was served
  temporarily on loopback port 5175 without changing the working tree or backend.
- TypeScript, ESLint, 28 unit tests and 44 Playwright tests passed. Coverage
  includes interrupted navigation retaining the composer DOM, reduced motion,
  preference changes during dialog exit, genuine fixture SSE lifecycle,
  attachment handling, task scrolling, approval decisions and QR states.

### Measured frame pacing

Live requestAnimationFrame samples during the first recorded viewport matrix
(before final focus/composer and tablet milestone refinements):

| Viewport | Samples | Median |    p95 | Frames >50 ms |
| -------- | ------: | -----: | -----: | ------------: |
| 1440x900 |    3731 | 8.3 ms | 8.7 ms |             2 |
| 1280x800 |    3785 | 8.3 ms | 9.1 ms |             2 |
| 930x581  |    3790 | 8.3 ms | 9.0 ms |             2 |
| 768x1024 |    3782 | 8.3 ms | 9.3 ms |             2 |
| 390x844  |    3391 | 8.3 ms | 9.3 ms |             3 |

An unrecorded live 1440x900 navigation/sidebar profile had 1066 samples:
median 8.3 ms, p95 9.1 ms, maximum 99.9 ms, seven frames above 25 ms and
three above 50 ms. Target-scoped Chromium metrics measured 338 ms of script,
45 ms of layout and 212 ms of style recalculation across that interaction sample.
Used JS heap at its end was approximately 14.3 MB.
The first recorded matrix included a maximum frame gap of 340.8 ms.

The final isolated production-build walkthrough measured p95 16.7 ms, with 1/2069
recorded and 1/144 unrecorded samples above 25 ms. These are observations on
the test environment, not guaranteed frame rates. Different refresh rates and
recording overhead make direct FPS comparisons inappropriate.

### Limits and reproduction

Occasional long main-thread frames remain in the live development profile;
this is not a claim of stutter-free or guaranteed 120 FPS. WebM captures run
at 25 FPS, supplementing rather than replacing frame-timing measurements.
The 930x581 viewport is encoded at 930x580 because the codec requires even
dimensions. Measured document overflow was zero at every recorded size;
small nonzero layout-shift scores (0.0025-0.0123) were observed.

Full QR success still needs the backend-owned private TLS gateway. Actual device
connection changes and pending production approval motion cannot be certified
when the corresponding live states are absent.

Run the normal checks from this directory:

```sh
npm run typecheck
npm run lint
npm run test
npm run build
THOZHA_E2E_PYTHON=/path/to/backend/.venv/bin/python3 \
THOZHA_E2E_BACKEND=/path/to/backend/worktree npm run test:e2e
```

To record the isolated approval review, set `THOZHA_E2E_MOTION_CAPTURE` to an
absolute evidence directory and select `--grep 'reviews the exact operation'`.
Recording starts after fixture sign-in and always stops in `finally`.
Real-session capture scripts and WebM files are delivered as session artifacts,
not committed private browser data or repository test fixtures.
