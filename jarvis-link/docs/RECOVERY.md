# Recovery and operator notes

- Keep the relay deploy disabled until local protocol, replay, and revocation tests pass.
- Durable Object message state is authoritative; clients persist their last acknowledged sequence and retry with the same message ID/idempotency key.
- Expired queued messages are rejected; task approvals expire independently in the Jarvis supervisor.
- Restarting a host never replays an unacknowledged privileged action. The host must query supervisor state, verify the exact approval/task digest, and resume only after explicit approval.
- If a device key is lost, revoke its device ID, create a new identity, and enroll it again. Never export the old private key or place it in a relay secret.
- Recovery procedures must not reset the existing OpenJarvis pairing or device credentials.
