# Cinematic workspace motion

The graphite information architecture, API contracts and authorization boundaries
are unchanged. Motion reflects real application state, never invented tokens,
progress percentages, approvals, connectivity or pairing invitations.

## Why the first pass was hard to see

The owner-authenticated app served the source from `e98ba32d`: its loaded
stylesheet matched that commit. The page was visible and focused, the operating
system/browser reported normal motion, the application preference was unset,
and its breathing animation was running. Accessibility settings were not the
cause. The faint contours and small welcome SVG simply had insufficient visual
weight.

The development server's revision header still contained its startup revision.
That header is not evidence that Vite's HMR source is stale. No backend process,
origin policy or accessibility preference was changed to obtain this result.

## Shared choreography

[`motion.css`](./src/motion.css) owns the CSS vocabulary.
[`motion.ts`](./src/components/motion.ts) provides matching Web Animation
timings, eligibility, retained-scene arrival and identity precedence.

| Purpose                                      | Timing                |
| -------------------------------------------- | --------------------- |
| Press feedback                               | 110 ms                |
| Controls and focus                           | 180 ms                |
| Panels, navigation, dialogs and new messages | 280 ms                |
| Route and conversation arrival               | 320 ms                |
| Dialog dismissal                             | 160 ms                |
| Welcome typography                           | 600-680 ms, staggered |
| Identity entrance / hero-to-presence handoff | 680 / 440 ms          |

The common easing is `cubic-bezier(.22,.8,.24,1)`. Resizing uses a restrained,
non-overshooting spring-like curve. There is no audio or haptic behavior.

- Native route snapshots move directionally with scale and slight perspective.
  The sidebar remains live outside the snapshot; subsequent navigation interrupts
  the preceding transition. Unsupported browsers change routes immediately and
  retain content-arrival effects.
- Contextual history travels its full width. It remains separate from persistent
  primary navigation. Chat and its unsent draft are not remounted on route changes.
- Anchored menus preserve their trigger origins. Dialogs enter with controlled
  translation/scale and become inert during dismissal. Escape, focus restoration
  and reduced-motion changes cannot strand an inert top-layer dialog.
- Only new message IDs receive entrance motion, not every SSE chunk or historical
  replay. Actual incremental deltas retain requestAnimationFrame batching;
  buffered output appears whole. Markdown remains selectable DOM.
- The welcome composition stages the sculpture, eyebrow, heading and suggestions.
  The composer remains fully opaque with stationary hitboxes: its entrance only
  changes a bounded shadow. Moving Send targets or fading command-menu text caused
  real interaction/accessibility failures during development and were removed.
- Sidebar width and bounded textarea resizing remain deliberate layout-animation
  exceptions. Task history and execution details still own their independent
  scrolling; the app shell stays stationary.

## Dimensional silver identity

[`SilverIntelligence.tsx`](./src/components/SilverIntelligence.tsx) retains a
bounded renderer while the welcome sculpture becomes compact response presence.
Task details use the same material and vocabulary. The existing small Living
Pulse remains in the startup handoff and compact application marks.

[`renderer.ts`](./src/silver/renderer.ts) uses raw WebGL without a new dependency:

- A deforming mesh with 3,625 vertices and 6,912 triangles; no raymarching,
  particles or full-screen shader.
- Analytic studio reflections, directional highlights, depth and a bounded
  grounding shadow. No continuous full-screen blur.
- A maximum 720x420 drawing buffer and capped device pixel ratio of 1.5.
- Native RAF scheduling with approximately 60 Hz ambient drawing, not a promise
  of 120 FPS. GPU frames do not cause React state updates.
- One-RAF deferred initialization avoids duplicate Strict Mode GPU startup.
  A static metallic SVG crossfades into the canvas.
- Explicit warnings and static fallback on initialization/context failure;
  context restoration recreates resources. Cleanup releases GPU resources,
  observers, listeners and scheduled frames.

| Actual state                                        | Visible treatment                                   |
| --------------------------------------------------- | --------------------------------------------------- |
| Idle                                                | Calm, continuously deforming liquid-silver loop     |
| Pending chat request / actual response generation   | Faster flow and stronger deformation                |
| Supervisor CREATED / PLANNING / RUNNING / VERIFYING | Purposeful directional light and tighter shape      |
| WAITING_APPROVAL / pending approval                 | More open contour and deliberate attention emphasis |
| Actual response or task completion                  | Resolution sweep and settling geometry              |
| Failure                                             | Restrained broken contour                           |
| Cancellation                                        | Dimmed, settling identity                           |

Terminal states stop continuous drawing after 1,200 ms. Historical messages do
not replay completion effects. PAUSED tasks are not presented as executing.
An active supervisor state takes precedence over terminal chat completion;
active chat generation remains responsive to the chat operation.

## Accessibility and resource ownership

System reduced motion or **Settings -> Visual preferences** disables decorative
CSS, Web Animations and scene snapshots. The sculpture draws a static state.
Changing preferences cancels active transitions. Neither operating-system nor
stored owner preferences are overridden.

Hidden documents, offscreen canvases and inactive retained chat pause rendering.
Normal motion resumes only when eligible. Visibility and context-loss recovery
are covered by browser regression tests. Existing keyboard behavior, focus,
modal locking, contrast checks and mobile navigation remain authoritative.

## Verification and evidence boundaries

The rework was checked on the actual owner-authenticated app at port 5174 and
against isolated backend/model fixtures. These are different kinds of evidence.

- Live recordings cover 1440x900, 1280x800, 930x581, 768x1024 and 390x844.
  Full frames were inspected, including short-height and mobile composition.
- The authorized harmless Java-interface prompt returned genuine HTTP 200
  `text/event-stream`, a real answer and persisted conversation history.
- One explicitly reviewed, read-only task returned HTTP 202 and reached real
  COMPLETED state. The saved answer acknowledged that it could not verify the
  macOS version; the frontend does not claim that a system command succeeded.
- That verification exposed a frontend Strict Mode lifecycle guard which never
  reset after effect cleanup. Resetting it in effect setup restores task preflight
  without changing task policy, auth, API or backend behavior.
- Actual port-5174 pairing returned 409, accurately showing that the private
  connection is not configured. No QR was fabricated.
- Production approvals were empty. Exact-operation review and explicit decisions
  are demonstrated only in the clearly identified isolated control fixture.
- The 24-second comparison uses exact published `e98ba32d` source and the rework,
  on the same temporary loopback origin, viewport, owner data and interaction
  schedule. Port 5175 receives a genuine 403 pairing-origin refusal in both clips;
  it is not the port-5174 gateway-unconfigured state.
- Comparison clips precede the authorized new chat/task, preserving identical
  history data. Later lifecycle/composer fixes are additionally verified in
  live responsive captures and browser tests.

The delivered session evidence includes the side-by-side WebM, continuous
walkthrough, separate real chat/task recordings, approved-origin pairing,
reduced motion, responsive clips, inspected frames and producing scripts.
Captures are not committed private browser data or test fixtures.

### Measurements, not frame-rate guarantees

On the actual app at 1440x900, an unrecorded nine-second sample contained 1,052
RAF intervals: median 8.3 ms, p95 9.1 ms, maximum 116.6 ms. Idle had one 25.8 ms
interval; Tasks and Chats navigation included 116.6 and 58.3 ms intervals.

A separate nine-second mixed sample had median 8.3 ms, p95 9.3 ms and an
unexplained 1,000.8 ms gap. Observed long tasks were 81, 79 and 63 ms; those do
not establish the cause of the one-second gap. CPU-side renderer initialization
measured 30.5 ms. Target-scoped script/layout/style deltas were approximately
438/29/173 ms; JS heap changed from 32.4 to 37.8 MB.

The final isolated production-build walkthrough measured p95 16.8 ms, with
5/2,175 recorded and 3/142 unrecorded intervals above 25 ms. An earlier run had
56/2,603 recorded intervals above 25 ms. Final desktop/mobile/reduced launch
checks measured p95 33.3/16.7/16.7 ms and maxima 133.4/99.9/100 ms respectively.
These workloads are not controlled comparisons.

Navigation and initialization hitches remain a limitation, not a claim of
stutter-free performance. WebM captures run at 25 FPS and cannot prove high
refresh-rate frame pacing. The 930x581 viewport encodes at 930x580 because the
codec requires even dimensions. Measured document overflow was zero at the
verified narrow viewports.

Full QR success and real device-connection changes still require backend-owned
availability. No production approval was manufactured to demonstrate animation.
PR #1 remains unmerged for integrated and visual acceptance.

## Reproduction

Run from this directory:

```sh
npm run typecheck
npm run lint
npm run test
npm run build
THOZHA_E2E_PYTHON=/path/to/backend/.venv/bin/python3 \
THOZHA_E2E_BACKEND=/path/to/backend/worktree npm run test:e2e
```

TypeScript, ESLint, production build, 30 unit tests and all 44 Playwright cases
passed. Coverage includes actual
changing/static sculpture pixels, retained composer identity, hidden rendering,
context restoration, interrupted navigation, dialog exit, SSE lifecycle,
attachments, independent scrolling, exact-operation review and genuine QR states.
Build before browser tests: the preview server reads `dist`, not HMR source.

To record isolated approval review, set `THOZHA_E2E_MOTION_CAPTURE` to an absolute
evidence directory and select `--grep 'reviews the exact operation'`. Recording
starts after fixture sign-in and always stops in `finally`.
