# Thozha

Thozha is the private monorepo for the existing Jarvis companion and backend. It preserves the independently maintained Android, Jarvis Link, and OpenJarvis histories under separate directories. The Web Workspace is owned separately by Copilot and is not included until its design is reviewed and approved.

## Repository layout

- android-companion/ — native Android companion; Codex-owned.
- jarvis-link/ — relay/host protocol and client sources; Codex-owned.
- openjarvis/ — Mac backend and supervisor; Codex-owned.
- contracts/ — versioned contracts verified against source.
- docs/COPILOT_INTEGRATION.md — Web ownership and integration guidance.
- web/ — reserved for the approved Copilot frontend; not yet published.

## Security and runtime boundaries

The Mac API is loopback-first. The Android companion authenticates with its paired-device credential and secure server identity. The companion gateway is a narrow allowlist, not a generic proxy. Supervisor approval remains required for operations that policy classifies as consequential. The Web frontend must not store paired-device bearer tokens or call loopback APIs directly.

The Jarvis Link v1 Worker contract is a prototype contract only; no production relay deployment is implied. The Live voice WebSocket currently supports the loopback/USB development path only. Neither is a public or USB-free remote Web API.

## Start here

- Companion HTTP API v1: contracts/companion-api/v1.md
- Jarvis Link wire protocol v1: contracts/jarvis-link/v1.md
- Copilot integration guide: docs/COPILOT_INTEGRATION.md
- Ownership and merge rules: OWNERSHIP.md
- Migration security review: SECURITY_AUDIT.md

Do not treat fixture data as live backend state. Do not claim an endpoint is supported unless the contract labels its source route and transport scope.
