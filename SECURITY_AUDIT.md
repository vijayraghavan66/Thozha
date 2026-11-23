# Thozha migration security audit

Audit date: 2026-11-23. This report covers the sanitized source-history copies prepared for the private Thozha monorepo. Original checkouts and histories remain unchanged.

## Included source revisions

- Android companion: source branch integration/telegram-ui, original HEAD 372a364.
- Jarvis Link: source branch fix/full-assistant-response, original HEAD 018276f.
- OpenJarvis: source branch main, original HEAD c4cd0c82. The original checkout had pre-existing dirty/untracked work; none of that work was staged or included.

The monorepo imports each project's reachable branch history under its own directory. Sanitization rewrites commit IDs in the export only; the original repositories retain their commits and branches.

## Sanitization performed on export copies

- Removed the tracked Android premium-audit.json from the exported history because it recorded an absolute home-directory path.
- Replaced the owner's absolute home prefix in exported text/history with a redacted path. OpenJarvis's current remote-development workspace defaults were also changed to derive from Path.home() in local commit c4cd0c82.
- Replaced historical AWS-key-shaped and JWT-shaped values in exported history. The confirmed JWT was a public-role sample, not a service-role credential; it is still omitted from the export. Two Google API-key-shaped strings in vendored Baileys history were also replaced in the monorepo copy.
- Excluded raw PROGRESS.md, local configuration, Firebase service-account files, signing material, Wrangler state, runtime databases, logs, captures, APKs and evaluation workspaces through the monorepo allowlist and .gitignore.

## Scan results

A pattern scan covered all reachable text and binary blobs in the filtered Android, Link and OpenJarvis histories. It found zero home-path matches, AWS access-key shapes, Google API-key shapes, GitHub PAT shapes, JWT shapes, Telegram bot-token shapes, or private-key blocks. Eight private-key header strings remain in OpenJarvis documentation/security-test material; source review confirmed they are header-only scanner/test examples with no private-key payload.

The scan also found six Slack-token-shaped strings in OpenJarvis redaction/security tests and documentation; targeted review classified them as synthetic fixtures used to verify log sanitization, not account credentials.

A separate read-only review of the unfiltered OpenJarvis history found no established live private key, device token, bot token, or privileged service credential. Its uncertain historical key-shaped documentation examples and the public-role JWT were replaced in the sanitized export. The original checkout was not rewritten.

## Scope and limitations

This is a source/history scan and targeted review, not a query against external credential providers. The Web mockup and standalone Thozha visual-review prototype are not included. The remote repository was confirmed private and empty before migration; no push has occurred in this checkpoint. Re-run the scan against the final staged tree immediately before pushing.
