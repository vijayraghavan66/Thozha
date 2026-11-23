# Parallel development handoff

## Branch ownership

- main is the reviewed integration baseline. Merge through pull requests; do not force-push.
- Codex works on codex/core: OpenJarvis, Android backend integration, Jarvis Link, security boundaries, persistence, and versioned contracts.
- Copilot works on copilot/web: only the approved React/TypeScript Web Workspace under web/.
- The MagicPath Web and Android concepts remain references until the owner approves the design. The standalone Android visual prototype is not the approved Web design and is not part of the baseline.

## Workspace setup

Create separate worktrees from the published baseline:

    git fetch origin
    git worktree add ../Thozha-codex codex/core
    git worktree add ../Thozha-copilot copilot/web

If a branch is not present yet, create it from main and push it without force:

    git switch -c codex/core origin/main
    git push -u origin codex/core

Copilot should use the analogous copilot/web branch. Never have both agents edit the same worktree.

## File boundaries

- Codex: openjarvis/, android-companion/, jarvis-link/, contracts/, root build/security documentation.
- Copilot: web/ only after design approval. Copilot may add frontend-specific tests under web/.
- Shared interfaces: Codex edits contracts/; Copilot consumes them and requests changes through a review. Do not duplicate API schemas in UI components.
- Keep local credentials, pairing state, Keystore files, PROGRESS.md, databases, logs, model weights, APKs, and .wrangler/ out of Git.

## Integration and verification

1. Copilot uses explicit Preview fixtures until a browser-safe auth/session contract is reviewed. Never put a paired-device bearer token in browser storage.
2. Copilot maps interactions only to routes marked implemented in contracts/companion-api/v1.md.
3. Codex reviews backend/auth/approval changes and updates contracts before the Web adapter consumes them.
4. Integration PRs must run relevant Android, OpenJarvis, Jarvis Link, and Web checks. Security-sensitive actions retain supervisor approval and device binding.
5. Use persisted task/approval states only after an approved authenticated Web transport exists. Preview data must remain labeled and cannot simulate an approval or completed operation.
