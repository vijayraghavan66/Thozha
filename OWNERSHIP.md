# Ownership and merge rules

## Codex

Codex owns branch codex/core and:

- `openjarvis/`: Mac API, supervisor, task execution, approval enforcement, notifications, and service reliability.
- `android-companion/`: Android API client, authentication, task state, approvals, voice transport, and native integration.
- `jarvis-link/`: relay/host protocol, cryptographic envelopes, delivery, replay protection, and host integration.
- `contracts/`, repository README, and integration/security documentation.

Codex reviews contract changes that affect Android or Web consumers.

## Copilot

Copilot owns branch copilot/web and the Web Workspace frontend under web/ after the owner approves its design. Until approval:

- Keep the interactive Web mockup in its separate prototype workspace.
- Do not add or replace the approved Web design in this repository.
- Use clearly labeled local preview data only.
- Do not edit `openjarvis/`, `android-companion/`, `jarvis-link/`, or `contracts/`.

## Change coordination

1. Contract changes are proposed and reviewed before the frontend consumes them.
2. The frontend uses a typed adapter against versioned contracts; no direct database or supervisor access.
3. Codex integrates reviewed Web changes after source-level API and security review.
4. No shared-file concurrent edits. Avoid force-pushes. Keep unrelated dirty work in its original checkout.
