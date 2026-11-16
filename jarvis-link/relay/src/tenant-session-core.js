const enc = new TextEncoder();
const DEVICE_ID_RE = /^[0-9a-f]{32}$/;
const NONCE_RE = /^[A-Za-z0-9_-]{32,128}$/;
const KIND_SET = new Set([
  "task.submit", "task.progress", "task.result", "approval.request", "approval.decision", "task.cancel",
]);
const MAX_ENVELOPE_BYTES = 64 * 1024;
const MAX_TTL_MS = 24 * 60 * 60 * 1000;
const REQUEST_SKEW_MS = 60_000;
const NONCE_TTL_MS = 2 * 60 * 1000;
const RATE_LIMIT_PER_MINUTE = 120;
const MAX_INBOX_MESSAGES = 1000;

export class TenantSessionCore {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.tenantId = null;
    this.lock = Promise.resolve();
  }

  async fetch(request) {
    try {
    const tenant = request.headers.get("X-Jarvis-Link-Resolved-Tenant") || "";
    if (!TENANT_RE.test(tenant)) return fail("INVALID_ENVELOPE", 400, "Invalid tenant route");
    this.tenantId = tenant;
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/v1/enrollment/bootstrap" && request.method === "POST") return await this.bootstrap(request);
    if (path === "/v1/enrollment/invitations" && request.method === "POST") return await this.createInvitation(request);
    if (path === "/v1/enrollment/challenge" && request.method === "POST") return await this.createChallenge(request);
    if (path === "/v1/enrollment/complete" && request.method === "POST") return await this.completeEnrollment(request);
    if (path === "/v1/enrollment/pending" && request.method === "GET") return await this.pendingEnrollments(request);
    if (path === "/v1/enrollment/certify" && request.method === "POST") return await this.certifyDevice(request);

    const certificateMatch = path.match(/^\/v1\/devices\/([0-9a-f]{32})\/certificate$/);
    if (certificateMatch && request.method === "GET") return await this.deviceCertificate(request, certificateMatch[1]);
    const pushMatch = path.match(/^\/v1\/devices\/([0-9a-f]{32})\/push-token$/);
    if (pushMatch && request.method === "PUT") return await this.setPushToken(request, pushMatch[1]);
    if (pushMatch && request.method === "DELETE") return await this.clearPushToken(request, pushMatch[1]);
    if (pushMatch && request.method === "GET") return await this.readPushToken(request, pushMatch[1]);
    const revokeMatch = path.match(/^\/v1\/devices\/([0-9a-f]{32})\/revoke$/);
    if (revokeMatch && request.method === "POST") return await this.revokeDevice(request, revokeMatch[1]);
    const inboxMatch = path.match(/^\/v1\/devices\/([0-9a-f]{32})\/inbox$/);
    if (inboxMatch && request.method === "GET") return await this.inbox(request, inboxMatch[1], url);
    const streamMatch = path.match(/^\/v1\/devices\/([0-9a-f]{32})\/stream$/);
    if (streamMatch && request.method === "GET") return await this.stream(request, streamMatch[1]);
    if (path === "/v1/messages" && request.method === "POST") return await this.sendMessage(request);
    const ackMatch = path.match(/^\/v1\/messages\/([0-9a-f]{32})\/ack$/);
    if (ackMatch && request.method === "POST") return await this.ack(request, ackMatch[1]);
    return fail("NOT_FOUND", 404, "Relay route not found");
    } catch (exc) {
      if (exc instanceof RelayError) return fail(exc.code, exc.status, exc.message);
      return fail("INTERNAL", 500, "Relay request failed safely");
    }
  }

  async bootstrap(request) {
    const supplied = request.headers.get("X-Jarvis-Link-Bootstrap-Key") || "";
    const expected = this.env.JARVIS_LINK_BOOTSTRAP_SECRET || "";
    if (!expected || !constantEqual(supplied, expected)) return fail("UNAUTHORIZED", 401, "Bootstrap authorization rejected");
    const body = await readJson(request, 16 * 1024);
    const certificate = body.certificate;
    if (!certificate || body.tenant_id !== this.tenantId || certificate.tenant_id !== this.tenantId || certificate.device_role !== "host") return fail("INVALID_ENROLLMENT", 422, "Initial device must provide a self-signed host-root certificate for this tenant");
    const keys = await validatePublicKeys(certificate.signing_public_key_b64url, certificate.encryption_public_key_b64url);
    const deviceId = await deriveDeviceId(keys.signingDer);
    const now = Date.now();
    const name = validateDeviceName(body.device_name || "Jarvis Host");
    if (certificate.device_id !== deviceId || certificate.issuer_device_id !== deviceId) return fail("INVALID_CERTIFICATE", 422, "Bootstrap host certificate ID/issuer mismatch");
    const certFailure = await verifyCertificateAgainstKey(certificate, certificate.signing_public_key_b64url, this.tenantId, now);
    if (certFailure) return fail("INVALID_CERTIFICATE", 422, certFailure);
    await this.atomic(async () => {
      const tenant = await this.get("tenant");
      if (tenant) throw new RelayError("CONFLICT", 409, "Tenant host is already bootstrapped");
      const record = { id: deviceId, tenant_id: this.tenantId, device_name: name, device_role: "host", active: true, root: true, certificate, signing_public_key_b64url: certificate.signing_public_key_b64url, encryption_public_key_b64url: certificate.encryption_public_key_b64url, revoked_at: null };
      await this.put(`device:${deviceId}`, record);
      await this.put("tenant", { tenant_id: this.tenantId, host_root_device_id: deviceId, host_root_signing_public_key_b64url: certificate.signing_public_key_b64url });
    });
    return ok({ tenant_id: this.tenantId, device_id: deviceId, status: "active", certificate });
  }

  async createInvitation(request) {
    const auth = await this.authenticate(request);
    if (auth.device.device_role !== "host") return fail("UNAUTHORIZED", 403, "Only a host can invite devices");
    const body = await readJson(request, 8 * 1024);
    const role = body.device_role;
    if (!new Set(["companion", "host"]).has(role)) return fail("INVALID_ENROLLMENT", 422, "Unsupported device role");
    const ttl = Number(body.expires_in_seconds ?? 600);
    if (!Number.isInteger(ttl) || ttl < 30 || ttl > 600) return fail("INVALID_ENROLLMENT", 422, "Invitation lifetime must be 30–600 seconds");
    const invitationId = randomHex(16);
    const secret = randomBytes(32);
    const expiresAt = Date.now() + ttl * 1000;
    await this.atomic(async () => {
      await this.put(`invite:${invitationId}`, {
        id: invitationId, tenant_id: this.tenantId, role,
        secret_hash: await sha256Hex(secret), expires_at_unix_ms: expiresAt,
        created_by: auth.device.id, status: "open",
      });
    });
    return ok({ invitation_id: invitationId, invitation_secret_b64url: b64url(secret), expires_at_unix_ms: expiresAt });
  }

  async createChallenge(request) {
    const body = await readJson(request, 16 * 1024);
    const invitationId = body.invitation_id;
    if (!ID_RE.test(invitationId || "")) return fail("INVALID_ENROLLMENT", 422, "Invalid invitation ID");
    const name = validateDeviceName(body.device_name);
    if (!new Set(["host", "companion"]).has(body.device_role)) return fail("INVALID_ENROLLMENT", 422, "Unsupported device role");
    const clientNonce = body.client_nonce_b64url || "";
    if (!NONCE_RE.test(clientNonce)) return fail("INVALID_ENROLLMENT", 422, "Invalid client nonce");
    const keys = await validatePublicKeys(body.signing_public_key_b64url, body.encryption_public_key_b64url);
    const deviceId = await deriveDeviceId(keys.signingDer);
    const secret = unb64url(body.invitation_secret_b64url || "");
    if (!secret || secret.length !== 32) return fail("UNAUTHORIZED", 401, "Invitation authorization rejected");
    const challengeId = randomHex(16);
    const challenge = randomBytes(32);
    const expires = Date.now() + 5 * 60 * 1000;
    await this.atomic(async () => {
      const invite = await this.get(`invite:${invitationId}`);
      if (!invite || invite.status !== "open" || invite.expires_at_unix_ms <= Date.now()) throw new RelayError("INVITATION_EXPIRED", 410, "Invitation expired or already used");
      if (invite.tenant_id !== this.tenantId || invite.role !== body.device_role || invite.secret_hash !== await sha256Hex(secret)) throw new RelayError("UNAUTHORIZED", 401, "Invitation authorization rejected");
      const existing = await this.get(`device:${deviceId}`);
      if (existing?.active && !existing.revoked_at) throw new RelayError("CONFLICT", 409, "Device is already enrolled");
      const pending = {
        challenge_id: challengeId, invitation_id: invitationId, tenant_id: this.tenantId,
        device_id: deviceId, device_name: name, device_role: body.device_role,
        signing_public_key_b64url: body.signing_public_key_b64url,
        encryption_public_key_b64url: body.encryption_public_key_b64url,
        client_nonce_b64url: clientNonce, challenge_b64url: b64url(challenge),
        expires_at_unix_ms: expires, status: "challenged",
      };
      await this.put(`enroll:${challengeId}`, pending);
      invite.status = "challenged";
      invite.challenge_id = challengeId;
      await this.put(`invite:${invitationId}`, invite);
    });
    return ok({ challenge_id: challengeId, challenge_b64url: b64url(challenge), device_id: deviceId, expires_at_unix_ms: expires });
  }

  async completeEnrollment(request) {
    const body = await readJson(request, 16 * 1024);
    const challengeId = body.challenge_id;
    if (!ID_RE.test(challengeId || "")) return fail("INVALID_ENROLLMENT", 422, "Invalid challenge ID");
    if (body.tenant_id !== this.tenantId) return fail("INVALID_ENROLLMENT", 422, "Enrollment tenant does not match invitation");
    const pending = await this.get(`enroll:${challengeId}`);
    if (!pending || pending.tenant_id !== this.tenantId || pending.status !== "challenged" || pending.expires_at_unix_ms <= Date.now()) return fail("MESSAGE_EXPIRED", 410, "Enrollment challenge expired or already used");
    const same = ["invitation_id", "device_id", "device_name", "device_role", "signing_public_key_b64url", "encryption_public_key_b64url", "client_nonce_b64url", "challenge_b64url"]
      .every((field) => body[field] === pending[field]);
    if (!same) return fail("INVALID_ENROLLMENT", 422, "Enrollment fields do not match the challenge");
    const bytes = enrollmentBytes(pending);
    if (!await verifyP256(pending.signing_public_key_b64url, body.signature_b64url, bytes)) return fail("INVALID_SIGNATURE", 401, "Enrollment proof rejected");
    await this.atomic(async () => {
      const current = await this.get(`enroll:${challengeId}`);
      const invite = await this.get(`invite:${pending.invitation_id}`);
      if (!current || current.status !== "challenged" || !invite || invite.status !== "challenged" || invite.challenge_id !== challengeId || invite.expires_at_unix_ms <= Date.now()) throw new RelayError("NONCE_REPLAY", 409, "Enrollment invitation was already consumed");
      current.status = "pending_host_certification";
      current.completed_at_unix_ms = Date.now();
      await this.put(`enroll:${challengeId}`, current);
      invite.status = "pending_host_certification";
      await this.put(`invite:${pending.invitation_id}`, invite);
    });
    return ok({ tenant_id: this.tenantId, device_id: pending.device_id, status: "PENDING_HOST_CERTIFICATION" });
  }

  async pendingEnrollments(request) {
    const auth = await this.authenticate(request);
    if (auth.device.device_role !== "host") return fail("UNAUTHORIZED", 403, "Only a host can review enrollments");
    const entries = await this.state.storage.list({ prefix: "enroll:" });
    const pending = [];
    for (const item of entries.values()) {
      if (item.status === "pending_host_certification" && item.expires_at_unix_ms > Date.now()) {
        pending.push({ challenge_id: item.challenge_id, invitation_id: item.invitation_id, device_id: item.device_id, device_name: item.device_name, device_role: item.device_role, signing_public_key_b64url: item.signing_public_key_b64url, encryption_public_key_b64url: item.encryption_public_key_b64url, expires_at_unix_ms: item.expires_at_unix_ms });
      }
    }
    return ok({ enrollments: pending });
  }

  async certifyDevice(request) {
    const auth = await this.authenticate(request);
    if (!auth.device.root || auth.device.device_role !== "host") return fail("UNAUTHORIZED", 403, "Only the tenant host root can certify devices");
    const body = await readJson(request, 16 * 1024);
    const certificate = body.certificate;
    const certFailure = await this.verifyCertificate(certificate);
    if (certFailure) return fail("INVALID_CERTIFICATE", 422, certFailure);
    if (certificate.issuer_device_id !== auth.device.id || certificate.tenant_id !== this.tenantId) return fail("UNAUTHORIZED", 403, "Certificate issuer or tenant mismatch");
    const entries = await this.state.storage.list({ prefix: "enroll:" });
    const candidate = [...entries.values()].find((entry) => entry.status === "pending_host_certification" && entry.device_id === certificate.device_id);
    if (!candidate) return fail("NOT_FOUND", 404, "No matching pending enrollment");
    const expected = {
      device_role: candidate.device_role,
      signing_public_key_b64url: candidate.signing_public_key_b64url,
      encryption_public_key_b64url: candidate.encryption_public_key_b64url,
    };
    for (const [key, value] of Object.entries(expected)) if (certificate[key] !== value) return fail("INVALID_CERTIFICATE", 422, "Certificate does not match the pending device keys");
    const active = {
      id: candidate.device_id, tenant_id: this.tenantId, device_name: candidate.device_name,
      device_role: candidate.device_role, signing_public_key_b64url: candidate.signing_public_key_b64url,
      encryption_public_key_b64url: candidate.encryption_public_key_b64url, active: true, root: false,
      revoked_at: null, certificate,
    };
    await this.atomic(async () => {
      const current = await this.get(`enroll:${candidate.challenge_id}`);
      if (!current || current.status !== "pending_host_certification") throw new RelayError("NONCE_REPLAY", 409, "Enrollment already certified");
      current.status = "certified";
      await this.put(`enroll:${candidate.challenge_id}`, current);
      await this.put(`device:${candidate.device_id}`, active);
      const invite = await this.get(`invite:${candidate.invitation_id}`);
      if (invite) { invite.status = "consumed"; await this.put(`invite:${candidate.invitation_id}`, invite); }
    });
    return ok({ tenant_id: this.tenantId, device_id: candidate.device_id, status: "active", certificate });
  }

  /**
   * A device may register or clear ONLY its own push token; the signature identifies the device, so a caller
   * cannot register a token for someone else's device. The token is a delivery address, not a credential.
   */
  async setPushToken(request, deviceId) {
    const auth = await this.authenticate(request);
    if (auth.device.id !== deviceId) return fail("NOT_RECIPIENT", 403, "A device can only register its own push token");
    const body = await readJson(request, 4 * 1024);
    if (Object.keys(body).some((key) => key !== "token")) return fail("INVALID_ENVELOPE", 422, "Unexpected push-token field");
    if (typeof body.token !== "string" || !/^[A-Za-z0-9:_-]{100,4096}$/.test(body.token)) return fail("INVALID_ENVELOPE", 422, "Push token format is invalid");
    return this.atomic(async () => {
      const device = await this.get(`device:${deviceId}`);
      device.push_token = body.token;
      device.push_token_updated_at_unix_ms = Date.now();
      await this.put(`device:${deviceId}`, device);
      return ok({ device_id: deviceId, status: "registered" });
    });
  }

  async clearPushToken(request, deviceId) {
    const auth = await this.authenticate(request);
    if (auth.device.id !== deviceId) return fail("NOT_RECIPIENT", 403, "A device can only clear its own push token");
    return this.atomic(async () => {
      const device = await this.get(`device:${deviceId}`);
      delete device.push_token;
      delete device.push_token_updated_at_unix_ms;
      await this.put(`device:${deviceId}`, device);
      return ok({ device_id: deviceId, status: "cleared" });
    });
  }

  /** Only the tenant host may read a companion's push token, and only to send a wake-up. */
  async readPushToken(request, deviceId) {
    const auth = await this.authenticate(request);
    if (auth.device.device_role !== "host") return fail("UNAUTHORIZED", 403, "Only the host can read push tokens");
    const device = await this.get(`device:${deviceId}`);
    if (!device?.active || device.revoked_at || device.tenant_id !== auth.device.tenant_id || !device.push_token) return fail("NOT_FOUND", 404, "No push token for this device");
    return ok({ device_id: deviceId, token: device.push_token });
  }

  async deviceCertificate(request, deviceId) {
    const auth = await this.authenticate(request);
    const target = await this.get(`device:${deviceId}`);
    if (!target?.active || target.revoked_at) return fail("NOT_FOUND", 404, "Device certificate not found");
    if (auth.device.tenant_id !== target.tenant_id) return fail("NOT_FOUND", 404, "Device certificate not found");
    return ok({ certificate: target.certificate });
  }

  async revokeDevice(request, deviceId) {
    const auth = await this.authenticate(request);
    if (!auth.device.root || auth.device.device_role !== "host") return fail("UNAUTHORIZED", 403, "Only the host root can revoke devices");
    const body = await readJson(request, 4 * 1024);
    if (Object.keys(body).some((key) => key !== "reason_code")) return fail("INVALID_ENVELOPE", 422, "Unexpected revocation field");
    return this.atomic(async () => {
      const device = await this.get(`device:${deviceId}`);
      if (!device || device.tenant_id !== this.tenantId) return fail("NOT_FOUND", 404, "Device not found");
      if (device.revoked_at) return ok({ device_id: deviceId, status: "revoked" });
      device.active = false;
      device.revoked_at = Date.now();
      await this.put(`device:${deviceId}`, device);
      const ids = await this.get(`inbox:${deviceId}`) || [];
      for (const id of ids) {
        const msg = await this.get(`message:${id}`);
        if (msg && msg.status !== "ACKED") { msg.status = "UNDELIVERABLE"; await this.put(`message:${id}`, msg); }
      }
      return ok({ device_id: deviceId, status: "revoked" });
    });
  }

  async sendMessage(request) {
    const raw = new Uint8Array(await request.clone().arrayBuffer());
    if (raw.length > MAX_ENVELOPE_BYTES) return fail("INVALID_ENVELOPE", 413, "Envelope exceeds the relay limit");
    const auth = await this.authenticate(request, raw);
    let envelope;
    try { envelope = JSON.parse(new TextDecoder().decode(raw)); }
    catch { return fail("INVALID_ENVELOPE", 400, "Malformed message envelope"); }
    const validation = await this.validateEnvelope(envelope, auth.device);
    if (validation) return fail(validation.code, validation.status, validation.message);
    const id = envelope.message_id;
    const rawDigest = await sha256Hex(raw);
    const existing = await this.get(`message:${id}`);
    if (existing) {
      if (existing.raw_digest !== rawDigest) return fail("MESSAGE_REPLAY", 409, "Message ID was reused with different bytes");
      if (existing.status === "EXPIRED" || !existing.envelope) return fail("MESSAGE_EXPIRED", 410, "Message has expired");
      return ok({ message_id: id, status: existing.status.toLowerCase(), expires_at_unix_ms: existing.envelope.expires_at_unix_ms });
    }
    return this.atomic(async () => {
      const race = await this.get(`message:${id}`);
      if (race) {
        if (race.raw_digest !== rawDigest) throw new RelayError("MESSAGE_REPLAY", 409, "Message ID was reused with different bytes");
        if (race.status === "EXPIRED" || !race.envelope) throw new RelayError("MESSAGE_EXPIRED", 410, "Message has expired");
        return ok({ message_id: id, status: race.status.toLowerCase(), expires_at_unix_ms: race.envelope.expires_at_unix_ms });
      }
      const target = await this.get(`device:${envelope.to_device_id}`);
      if (!target?.active || target.revoked_at || target.tenant_id !== this.tenantId) throw new RelayError("NOT_RECIPIENT", 404, "Recipient is unavailable");
      const queue = await this.get(`inbox:${envelope.to_device_id}`) || [];
      if (queue.length >= MAX_INBOX_MESSAGES) throw new RelayError("RATE_LIMITED", 429, "Recipient queue is full");
      const record = { envelope, to_device_id: envelope.to_device_id, raw_digest: rawDigest, status: "QUEUED", created_at_unix_ms: Date.now() };
      await this.put(`message:${id}`, record);
      queue.push(id);
      await this.put(`inbox:${envelope.to_device_id}`, queue);
      await this.scheduleExpiry();
      this.notifySockets(envelope.to_device_id);
      return ok({ message_id: id, status: "queued", expires_at_unix_ms: envelope.expires_at_unix_ms });
    });
  }

  async inbox(request, deviceId, url) {
    const auth = await this.authenticate(request);
    if (auth.device.id !== deviceId) return fail("NOT_RECIPIENT", 403, "Cannot read another device inbox");
    const limit = Number(url.searchParams.get("limit") || 20);
    if (!Number.isInteger(limit) || limit < 1 || limit > 50) return fail("INVALID_ENVELOPE", 422, "Invalid inbox limit");
    return this.atomic(async () => {
      const ids = await this.get(`inbox:${deviceId}`) || [];
      const messages = [];
      const remaining = [];
      for (const id of ids) {
        const record = await this.get(`message:${id}`);
        if (!record || record.status === "ACKED" || record.status === "UNDELIVERABLE" || record.status === "EXPIRED") continue;
        if (record.envelope.expires_at_unix_ms <= Date.now()) {
          record.status = "EXPIRED";
          await this.put(`message:${id}`, record);
          continue;
        }
        remaining.push(id);
        if (messages.length >= limit) continue;
        record.status = "DELIVERED";
        record.delivered_at_unix_ms ||= Date.now();
        await this.put(`message:${id}`, record);
        const sender = await this.get(`device:${record.envelope.from_device_id}`);
        if (!sender?.active || sender.revoked_at) continue;
        messages.push({ envelope: record.envelope, sender_certificate: sender.certificate });
      }
      await this.put(`inbox:${deviceId}`, remaining);
      return ok({ messages, next_cursor: null });
    });
  }

  async ack(request, messageId) {
    const body = await readJson(request, 4 * 1024);
    if (Object.keys(body).some((key) => !["status", "result_digest"].includes(key))) return fail("INVALID_ENVELOPE", 422, "Unexpected ACK field");
    if (!["received", "rejected"].includes(body.status)) return fail("INVALID_ENVELOPE", 422, "ACK status must be received or rejected");
    if (body.result_digest && !/^[0-9a-f]{64}$/.test(body.result_digest)) return fail("INVALID_ENVELOPE", 422, "Invalid result digest");
    const auth = await this.authenticate(request);
    return this.atomic(async () => {
      const record = await this.get(`message:${messageId}`);
      const recipient = record?.envelope?.to_device_id ?? record?.to_device_id;
      if (!record || recipient !== auth.device.id) return fail("NOT_FOUND", 404, "Message not found");
      if (record.status === "ACKED") {
        if (record.ack?.status === body.status && record.ack?.result_digest === (body.result_digest || "")) return ok({ message_id: messageId, status: "acked" });
        return fail("MESSAGE_REPLAY", 409, "Message already has a different ACK");
      }
      if (record.status === "EXPIRED" || !record.envelope || record.envelope.expires_at_unix_ms <= Date.now()) { record.status = "EXPIRED"; await this.put(`message:${messageId}`, record); return fail("MESSAGE_EXPIRED", 410, "Message has expired"); }
      if (!["QUEUED", "DELIVERED"].includes(record.status)) return fail("MESSAGE_REPLAY", 409, "Message is no longer acknowledgeable");
      record.status = "ACKED";
      record.ack = { status: body.status, result_digest: body.result_digest || "", acked_at_unix_ms: Date.now() };
      await this.put(`message:${messageId}`, record);
      return ok({ message_id: messageId, status: "acked" });
    });
  }

  async stream(request, deviceId) {
    const auth = await this.authenticate(request);
    if (auth.device.id !== deviceId) return fail("NOT_RECIPIENT", 403, "Cannot subscribe to another device stream");
    if (request.headers.get("upgrade")?.toLowerCase() !== "websocket") return fail("INVALID_ENVELOPE", 426, "WebSocket upgrade required");
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.state.acceptWebSocket(server);
    server.serializeAttachment({ device_id: deviceId, tenant_id: this.tenantId });
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws, message) {
    // WebSocket is only a wake-up channel. No command or payload is accepted.
    if (message === "ping") ws.send("pong");
    else ws.close(1008, "Use the signed HTTP protocol for relay operations");
  }

  async webSocketClose(ws, code, reason) { try { ws.close(code, reason); } catch {} }
  async webSocketError(ws) { try { ws.close(1011, "stream error"); } catch {} }

  async alarm() {
    await this.atomic(async () => {
      const entries = await this.state.storage.list({ prefix: "message:" });
      for (const [key, record] of entries) {
        if (record.envelope.expires_at_unix_ms <= Date.now() && record.status !== "ACKED") {
          record.status = "EXPIRED";
          record.envelope = null;
          await this.put(key, record);
        }
      }
      await this.scheduleExpiry();
    });
  }

  async authenticate(request, bodyBytes) {
    const deviceId = request.headers.get("X-Jarvis-Link-Device") || "";
    const tenant = request.headers.get("X-Jarvis-Link-Tenant") || "";
    const timestampText = request.headers.get("X-Jarvis-Link-Timestamp") || "";
    const nonce = request.headers.get("X-Jarvis-Link-Nonce") || "";
    const signature = request.headers.get("X-Jarvis-Link-Signature") || "";
    if (!DEVICE_ID_RE.test(deviceId) || tenant !== this.tenantId || !/^\d{13}$/.test(timestampText) || !NONCE_RE.test(nonce) || !signature) throw new RelayError("UNAUTHORIZED", 401, "Signed device request headers are required");
    const timestamp = Number(timestampText);
    const now = Date.now();
    if (Math.abs(now - timestamp) > REQUEST_SKEW_MS) throw new RelayError("UNAUTHORIZED", 401, "Signed request timestamp is outside the accepted window");
    const device = await this.get(`device:${deviceId}`);
    if (!device?.active || device.revoked_at || device.tenant_id !== this.tenantId) throw new RelayError("DEVICE_REVOKED", 401, "Device is not active");
    const bytes = bodyBytes || new Uint8Array(await request.clone().arrayBuffer());
    const bodyHash = await sha256Hex(bytes);
    const url = new URL(request.url);
    const pathAndQuery = url.pathname + url.search;
    const signed = `JARVIS-LINK-REQUEST-V1\n${tenant}\n${deviceId}\n${timestampText}\n${nonce}\n${request.method.toUpperCase()}\n${pathAndQuery}\n${bodyHash}`;
    const verified = await verifyP256(device.signing_public_key_b64url, signature, enc.encode(signed));
    if (!verified) throw new RelayError("INVALID_SIGNATURE", 401, "Device request signature rejected");
    await this.atomic(async () => {
      const nonceKey = `nonce:${deviceId}:${nonce}`;
      if (await this.get(nonceKey)) throw new RelayError("NONCE_REPLAY", 409, "Request nonce was already used");
      const windowKey = Math.floor(now / 60_000);
      const rateKey = `rate:${deviceId}:${windowKey}`;
      const rate = Number(await this.get(rateKey) || 0);
      if (rate >= RATE_LIMIT_PER_MINUTE) throw new RelayError("RATE_LIMITED", 429, "Device request rate limit exceeded");
      await this.put(nonceKey, now + NONCE_TTL_MS);
      await this.put(rateKey, rate + 1);
    });
    return { device, bodyBytes: bytes };
  }

  async validateEnvelope(envelope, sender) {
    if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) return { code: "INVALID_ENVELOPE", status: 400, message: "Envelope must be an object" };
    const required = ["v", "tenant_id", "message_id", "from_device_id", "to_device_id", "to_key_id", "kind", "issued_at_unix_ms", "expires_at_unix_ms", "ephemeral_pubkey_b64url", "iv_b64url", "ciphertext_b64url", "sender_signature_b64url"];
    if (Object.keys(envelope).length !== required.length || required.some((k) => !(k in envelope))) return { code: "INVALID_ENVELOPE", status: 422, message: "Envelope fields are invalid" };
    if (envelope.v !== 1 || envelope.tenant_id !== this.tenantId || !ID_RE.test(envelope.message_id) || !DEVICE_ID_RE.test(envelope.to_device_id) || envelope.from_device_id !== sender.id || !DEVICE_ID_RE.test(envelope.to_device_id)) return { code: "INVALID_ENVELOPE", status: 422, message: "Envelope identity fields are invalid" };
    if (!KIND_SET.has(envelope.kind)) return { code: "INVALID_ENVELOPE", status: 422, message: "Unsupported message kind" };
    const issued = Number(envelope.issued_at_unix_ms);
    const expires = Number(envelope.expires_at_unix_ms);
    const now = Date.now();
    if (!Number.isSafeInteger(issued) || !Number.isSafeInteger(expires) || issued > now + REQUEST_SKEW_MS || expires <= now || expires <= issued || expires - issued > MAX_TTL_MS) return { code: "MESSAGE_EXPIRED", status: 410, message: "Envelope time window is invalid or expired" };
    if (typeof envelope.to_key_id !== "string" || !/^[0-9a-f]{32}$/.test(envelope.to_key_id)) return { code: "INVALID_ENVELOPE", status: 422, message: "Recipient key ID is invalid" };
    const recipient = await this.get(`device:${envelope.to_device_id}`);
    if (!recipient?.active || recipient.revoked_at || recipient.tenant_id !== this.tenantId) return { code: "NOT_RECIPIENT", status: 404, message: "Recipient is unavailable" };
    const expectedKeyId = recipient.certificate?.certificate_id;
    if (envelope.to_key_id !== expectedKeyId) return { code: "INVALID_ENVELOPE", status: 422, message: "Recipient encryption key ID is not current" };
    const ephemeral = unb64url(envelope.ephemeral_pubkey_b64url);
    const iv = unb64url(envelope.iv_b64url);
    const cipher = unb64url(envelope.ciphertext_b64url);
    if (!ephemeral || !iv || !cipher || iv.length !== 12 || cipher.length < 16 || cipher.length > MAX_ENVELOPE_BYTES) return { code: "INVALID_ENVELOPE", status: 422, message: "Envelope cryptographic fields are invalid" };
    try {
      await crypto.subtle.importKey("spki", toArrayBuffer(ephemeral), { name: "ECDH", namedCurve: "P-256" }, false, []);
    } catch { return { code: "INVALID_ENVELOPE", status: 422, message: "Ephemeral key is invalid" }; }
    const bodyDigest = await sha256Hex(cipher);
    const signed = envelopeSigningString(envelope, bodyDigest);
    if (!await verifyP256(sender.signing_public_key_b64url, envelope.sender_signature_b64url, enc.encode(signed))) return { code: "INVALID_SIGNATURE", status: 401, message: "Envelope signature rejected" };
    return null;
  }

  async notifySockets(deviceId) {
    try {
      for (const ws of this.state.getWebSockets()) {
        const attachment = ws.deserializeAttachment();
        if (attachment?.device_id === deviceId && attachment?.tenant_id === this.tenantId) {
          try { ws.send(JSON.stringify({ type: "message_available" })); } catch {}
        }
      }
    } catch {}
  }

  async verifyCertificate(certificate) {
    if (!certificate || typeof certificate !== "object" || certificate.tenant_id !== this.tenantId || certificate.v !== 1 || !DEVICE_ID_RE.test(certificate.device_id || "") || !DEVICE_ID_RE.test(certificate.issuer_device_id || "") || !ID_RE.test(certificate.certificate_id || "") || !["host", "companion"].includes(certificate.device_role)) return "Certificate fields are invalid";
    const tenant = await this.get("tenant");
    if (!tenant) return "Tenant host root is not bootstrapped";
    if (certificate.issuer_device_id !== tenant.host_root_device_id) return "Certificate issuer is not the tenant host root";
    return verifyCertificateAgainstKey(certificate, tenant.host_root_signing_public_key_b64url, this.tenantId, Date.now());
  }

  async scheduleExpiry() {
    const items = await this.state.storage.list({ prefix: "message:" });
    let earliest = Infinity;
    for (const item of items.values()) if (item.status !== "ACKED" && item.status !== "EXPIRED" && item.envelope?.expires_at_unix_ms) earliest = Math.min(earliest, item.envelope.expires_at_unix_ms);
    if (earliest !== Infinity) await this.state.storage.setAlarm(earliest);
    else await this.state.storage.deleteAlarm();
  }

  get(key) { return this.state.storage.get(key); }
  put(key, value) { return this.state.storage.put(key, value); }
  /**
   * Serialises critical sections inside this tenant object. Deliberately NOT blockConcurrencyWhile():
   * on a real Durable Object an exception thrown inside it resets the whole object and loses the
   * typed RelayError, turning every expected rejection (replay, conflict, rate limit) into a 500.
   */
  atomic(callback) {
    const previous = this.lock || Promise.resolve();
    let release;
    this.lock = new Promise((resolve) => { release = resolve; });
    return previous.then(async () => { try { return await callback(); } finally { release(); } });
  }
}

const TENANT_RE = /^[A-Za-z0-9_-]{8,64}$/;
const ID_RE = /^[0-9a-f]{32}$/;

class RelayError extends Error {
  constructor(code, status, message) { super(message); this.code = code; this.status = status; }
}

async function readJson(request, maxBytes = MAX_ENVELOPE_BYTES) {
  const length = Number(request.headers.get("content-length") || 0);
  if (length > maxBytes) throw new RelayError("INVALID_ENVELOPE", 413, "Request body exceeds limit");
  const bytes = new Uint8Array(await request.clone().arrayBuffer());
  if (bytes.length > maxBytes) throw new RelayError("INVALID_ENVELOPE", 413, "Request body exceeds limit");
  let body;
  try { body = JSON.parse(new TextDecoder().decode(bytes)); }
  catch { throw new RelayError("INVALID_ENVELOPE", 400, "Malformed JSON body"); }
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new RelayError("INVALID_ENVELOPE", 400, "JSON body must be an object");
  return body;
}

function ok(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" } });
}

function fail(code, status, message) {
  return new Response(JSON.stringify({ error: { code, message, request_id: crypto.randomUUID() } }), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" } });
}

function b64url(bytes) { return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, ""); }
function unb64url(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]*$/.test(value)) return null;
  try { const b64 = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4); return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)); } catch { return null; }
}
function randomBytes(length) { const bytes = new Uint8Array(length); crypto.getRandomValues(bytes); return bytes; }
function randomHex(length) { return [...randomBytes(length)].map((x) => x.toString(16).padStart(2, "0")).join(""); }
function toArrayBuffer(bytes) { return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength); }
async function sha256(bytes) { return new Uint8Array(await crypto.subtle.digest("SHA-256", toArrayBuffer(bytes))); }
async function sha256Hex(bytes) { return [...await sha256(bytes)].map((x) => x.toString(16).padStart(2, "0")).join(""); }
async function deriveDeviceId(signingDer) { return [...(await sha256(signingDer)).slice(0, 16)].map((x) => x.toString(16).padStart(2, "0")).join(""); }

function constantEqual(a, b) {
  const x = enc.encode(String(a)); const y = enc.encode(String(b));
  if (x.length !== y.length) return false;
  let diff = 0; for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

function derEcdsaToP1363(der, size = 32) {
  // WebCrypto's ECDSA verifier takes fixed-width r||s. Android/Python use DER.
  if (!der || der.length < 8 || der[0] !== 0x30) return null;
  let i = 1;
  let seqLen = der[i++];
  if (seqLen & 0x80) {
    const n = seqLen & 0x7f;
    if (n < 1 || n > 2 || i + n > der.length) return null;
    seqLen = 0; for (let j = 0; j < n; j++) seqLen = (seqLen << 8) | der[i++];
  }
  if (i + seqLen !== der.length || der[i++] !== 0x02) return null;
  const rLen = der[i++]; if (rLen < 1 || i + rLen + 2 > der.length) return null;
  let r = der.slice(i, i + rLen); i += rLen;
  if (der[i++] !== 0x02) return null;
  const sLen = der[i++]; if (sLen < 1 || i + sLen !== der.length) return null;
  let s = der.slice(i, i + sLen);
  if (r[0] === 0) r = r.slice(1); if (s[0] === 0) s = s.slice(1);
  if (r.length > size || s.length > size) return null;
  const raw = new Uint8Array(size * 2);
  raw.set(r, size - r.length); raw.set(s, size * 2 - s.length);
  return raw;
}

async function verifyP256(publicB64, signatureB64Url, message) {
  const publicDer = unb64url(publicB64);
  const signatureDer = unb64url(signatureB64Url);
  if (!publicDer || !signatureDer) return false;
  const signature = derEcdsaToP1363(signatureDer);
  if (!signature) return false;
  try {
    const key = await crypto.subtle.importKey("spki", toArrayBuffer(publicDer), { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
    return await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, toArrayBuffer(signature), toArrayBuffer(message));
  } catch { return false; }
}

async function validatePublicKeys(signingB64, encryptionB64) {
  const signing = unb64url(signingB64); const encryption = unb64url(encryptionB64);
  if (!signing || !encryption) throw new RelayError("INVALID_ENVELOPE", 422, "Public keys are malformed");
  try {
    await crypto.subtle.importKey("spki", toArrayBuffer(signing), { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
    await crypto.subtle.importKey("spki", toArrayBuffer(encryption), { name: "ECDH", namedCurve: "P-256" }, false, []);
  } catch { throw new RelayError("INVALID_ENVELOPE", 422, "Public keys must be P-256 SPKI"); }
  return { signingDer: signing, encryptionDer: encryption };
}

function validateDeviceName(value) {
  if (typeof value !== "string" || !value.trim() || enc.encode(value).length > 64 || /[\u0000-\u001f\u007f\r\n]/.test(value)) throw new RelayError("INVALID_ENVELOPE", 422, "Device name must be 1–64 UTF-8 bytes without control characters");
  return value.trim();
}

function enrollmentBytes(pending) {
  return enc.encode(`JARVIS-LINK-ENROLL-V1\n${pending.tenant_id}\n${pending.invitation_id}\n${pending.challenge_id}\n${pending.device_id}\n${b64url(enc.encode(pending.device_name))}\n${pending.device_role}\n${pending.signing_public_key_b64url}\n${pending.encryption_public_key_b64url}\n${pending.client_nonce_b64url}\n${pending.challenge_b64url}`);
}

function certSigningString(cert) {
  return `JARVIS-LINK-DEVICE-CERT-V1\n${cert.v}\n${cert.tenant_id}\n${cert.device_id}\n${cert.device_role}\n${cert.signing_public_key_b64url}\n${cert.encryption_public_key_b64url}\n${cert.not_before_unix_ms}\n${cert.expires_at_unix_ms}\n${cert.certificate_id}\n${cert.issuer_device_id}`;
}

function envelopeSigningString(e, ciphertextDigest) {
  return `JARVIS-LINK-ENVELOPE-V1\n${e.tenant_id}\n${e.message_id}\n${e.from_device_id}\n${e.to_device_id}\n${e.to_key_id}\n${e.kind}\n${e.issued_at_unix_ms}\n${e.expires_at_unix_ms}\n${e.ephemeral_pubkey_b64url}\n${e.iv_b64url}\n${ciphertextDigest}`;
}

async function verifyCertificateAgainstKey(certificate, rootPublicB64, tenantId, now) {
  if (!certificate || typeof certificate !== "object" || certificate.v !== 1 || certificate.tenant_id !== tenantId || !DEVICE_ID_RE.test(certificate.device_id || "") || !DEVICE_ID_RE.test(certificate.issuer_device_id || "") || !ID_RE.test(certificate.certificate_id || "") || !["host", "companion"].includes(certificate.device_role)) return "Certificate fields are invalid";
  const signingDer = unb64url(certificate.signing_public_key_b64url || "");
  const encryptionDer = unb64url(certificate.encryption_public_key_b64url || "");
  if (!signingDer || !encryptionDer || certificate.device_id !== await deriveDeviceId(signingDer)) return "Certificate device ID or public keys are invalid";
  try {
    await crypto.subtle.importKey("spki", toArrayBuffer(signingDer), { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
    await crypto.subtle.importKey("spki", toArrayBuffer(encryptionDer), { name: "ECDH", namedCurve: "P-256" }, false, []);
  } catch { return "Certificate public keys are not P-256 SPKI"; }
  if (!Number.isSafeInteger(certificate.not_before_unix_ms) || !Number.isSafeInteger(certificate.expires_at_unix_ms) || certificate.not_before_unix_ms > now + REQUEST_SKEW_MS || certificate.expires_at_unix_ms <= now || certificate.expires_at_unix_ms - certificate.not_before_unix_ms > 366 * 24 * 60 * 60 * 1000) return "Certificate lifetime is invalid";
  if (certificate.device_role === "host" && certificate.issuer_device_id !== certificate.device_id) return "Bootstrap host certificate must be self-issued";
  if (!await verifyP256(rootPublicB64, certificate.signature_b64url, enc.encode(certSigningString(certificate)))) return "Host-root DeviceCertificate signature is invalid";
  return null;
}
