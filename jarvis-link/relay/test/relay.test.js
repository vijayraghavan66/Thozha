import test from "node:test";
import assert from "node:assert/strict";
import relay from "../src/handler.js";
import { TenantSessionCore } from "../src/tenant-session-core.js";

const te = new TextEncoder();
const TENANT = "tenant-test-000000000000000000000001";

class MemoryStorage {
  constructor() { this.values = new Map(); this.alarm = null; }
  async get(key) { const v = this.values.get(key); return v === undefined ? undefined : structuredClone(v); }
  async put(key, value) { this.values.set(key, structuredClone(value)); }
  async delete(key) { return this.values.delete(key); }
  async list({ prefix = "" } = {}) { return new Map([...this.values.entries()].filter(([k]) => k.startsWith(prefix)).map(([k, v]) => [k, structuredClone(v)])); }
  async setAlarm(time) { this.alarm = time; }
  async deleteAlarm() { this.alarm = null; }
  async getAlarm() { return this.alarm; }
}

class FakeState {
  constructor() { this.storage = new MemoryStorage(); this.sockets = []; }
  blockConcurrencyWhile(fn) { return fn(); }
  getWebSockets() { return this.sockets; }
}

function namespace(env) {
  const sessions = new Map();
  return {
    idFromName(name) { return name; },
    get(id) {
      if (!sessions.has(id)) sessions.set(id, new TenantSessionCore(new FakeState(), env));
      return { fetch: (request) => sessions.get(id).fetch(request) };
    },
    sessions,
  };
}

function b64url(bytes) { return Buffer.from(bytes).toString("base64url"); }
function fromB64url(text) { return new Uint8Array(Buffer.from(text, "base64url")); }
function rawToDer(raw) {
  let r = Buffer.from(raw.slice(0, 32)); let s = Buffer.from(raw.slice(32));
  while (r.length > 1 && r[0] === 0 && !(r[1] & 0x80)) r = r.subarray(1);
  while (s.length > 1 && s[0] === 0 && !(s[1] & 0x80)) s = s.subarray(1);
  if (r[0] & 0x80) r = Buffer.concat([Buffer.from([0]), r]);
  if (s[0] & 0x80) s = Buffer.concat([Buffer.from([0]), s]);
  const content = Buffer.concat([Buffer.from([2, r.length]), r, Buffer.from([2, s.length]), s]);
  return new Uint8Array(Buffer.concat([Buffer.from([0x30, content.length]), content]));
}
function derToRaw(der) {
  const b = Buffer.from(der); let i = 2; if (b[1] & 0x80) i += (b[1] & 0x7f) + 1;
  if (b[i++] !== 2) throw new Error("bad DER r"); const rl = b[i++]; let r = b.subarray(i, i + rl); i += rl;
  if (b[i++] !== 2) throw new Error("bad DER s"); const sl = b[i++]; let s = b.subarray(i, i + sl);
  if (r[0] === 0) r = r.subarray(1); if (s[0] === 0) s = s.subarray(1);
  const out = Buffer.alloc(64); r.copy(out, 32 - r.length); s.copy(out, 64 - s.length); return new Uint8Array(out);
}
async function sha256(bytes) { return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)); }
async function shaHex(bytes) { return Buffer.from(await sha256(bytes)).toString("hex"); }
async function sign(privateKey, message) { return b64url(rawToDer(new Uint8Array(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, privateKey, message)))); }
async function keyPair() { return crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]); }
async function ecdhPair() { return crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]); }
async function publicB64(pair) { return b64url(new Uint8Array(await crypto.subtle.exportKey("spki", pair.publicKey))); }
async function deviceIdFromPair(pair) { return Buffer.from((await sha256(new Uint8Array(await crypto.subtle.exportKey("spki", pair.publicKey)))).slice(0, 16)).toString("hex"); }
async function postRequest({ path, tenant = TENANT, deviceId, pair, body = null, bootstrapSecret = null, method = "POST", nonce = b64url(crypto.getRandomValues(new Uint8Array(32))), timestampOffsetMs = 0 }) {
  const bodyBytes = body === null ? new Uint8Array() : te.encode(JSON.stringify(body));
  const timestamp = String(Date.now() + timestampOffsetMs);
  const bodyHash = await shaHex(bodyBytes);
  const signed = `JARVIS-LINK-REQUEST-V1\n${tenant}\n${deviceId}\n${timestamp}\n${nonce}\n${method}\n${path}\n${bodyHash}`;
  const headers = { "X-Jarvis-Link-Tenant": tenant };
  if (deviceId && pair) {
    headers["X-Jarvis-Link-Device"] = deviceId;
    headers["X-Jarvis-Link-Timestamp"] = timestamp;
    headers["X-Jarvis-Link-Nonce"] = nonce;
    headers["X-Jarvis-Link-Signature"] = await sign(pair.privateKey, te.encode(signed));
  }
  if (bootstrapSecret) headers["X-Jarvis-Link-Bootstrap-Key"] = bootstrapSecret;
  if (body !== null) headers["Content-Type"] = "application/json";
  return new Request(`https://relay.test${path}`, { method, headers, body: method === "GET" ? undefined : bodyBytes });
}

function certBytes(cert) {
  return te.encode(`JARVIS-LINK-DEVICE-CERT-V1\n${cert.v}\n${cert.tenant_id}\n${cert.device_id}\n${cert.device_role}\n${cert.signing_public_key_b64url}\n${cert.encryption_public_key_b64url}\n${cert.not_before_unix_ms}\n${cert.expires_at_unix_ms}\n${cert.certificate_id}\n${cert.issuer_device_id}`);
}
function enrollmentBytes(p) {
  return te.encode(`JARVIS-LINK-ENROLL-V1\n${p.tenant_id}\n${p.invitation_id}\n${p.challenge_id}\n${p.device_id}\n${b64url(te.encode(p.device_name))}\n${p.device_role}\n${p.signing_public_key_b64url}\n${p.encryption_public_key_b64url}\n${p.client_nonce_b64url}\n${p.challenge_b64url}`);
}
function envelopeHeader(e) {
  return `JARVIS-LINK-ENVELOPE-V1\n${e.tenant_id}\n${e.message_id}\n${e.from_device_id}\n${e.to_device_id}\n${e.to_key_id}\n${e.kind}\n${e.issued_at_unix_ms}\n${e.expires_at_unix_ms}\n${e.ephemeral_pubkey_b64url}\n${e.iv_b64url}`;
}
async function makeEnvelope({ tenant, from, to, sender, toKeyId, kind = "task.submit", messageId = crypto.randomUUID().replaceAll("-", ""), text = "safe simulated task", issued = Date.now(), expires = issued + 60_000 }) {
  const ephemeral = await ecdhPair();
  const ephemeralDer = new Uint8Array(await crypto.subtle.exportKey("spki", ephemeral.publicKey));
  const recipientDer = fromB64url(to.encryption_public_key_b64url);
  const recipientKey = await crypto.subtle.importKey("spki", recipientDer, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const shared = new Uint8Array(await crypto.subtle.deriveBits({ name: "ECDH", public: recipientKey }, ephemeral.privateKey, 256));
  const msgIdBytes = Uint8Array.from(Buffer.from(messageId, "hex"));
  const info = te.encode(`JarvisLink/v1\n${tenant}\n${from}\n${to.id}`);
  const base = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const aesKey = await crypto.subtle.deriveKey({ name: "HKDF", hash: "SHA-256", salt: msgIdBytes, info }, base, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const header = {
    v: 1, tenant_id: tenant, message_id: messageId, from_device_id: from, to_device_id: to.id,
    to_key_id: toKeyId, kind, issued_at_unix_ms: issued, expires_at_unix_ms: expires,
    ephemeral_pubkey_b64url: b64url(ephemeralDer), iv_b64url: b64url(iv),
  };
  const aad = te.encode(envelopeHeader(header));
  const ciphertext = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: aad }, aesKey, te.encode(JSON.stringify({ objective: text }))));
  const full = { ...header, ciphertext_b64url: b64url(ciphertext) };
  full.sender_signature_b64url = await sign(sender.privateKey, te.encode(`${envelopeHeader(full)}\n${await shaHex(ciphertext)}`));
  return full;
}

async function setupTenant() {
  const env = { JARVIS_LINK_BOOTSTRAP_SECRET: "test-bootstrap-only" };
  env.SESSIONS = namespace(env);
  const hostSign = await keyPair(); const hostEcdh = await ecdhPair();
  const hostId = await deviceIdFromPair(hostSign);
  const hostCert = {
    v: 1, tenant_id: TENANT, device_id: hostId, device_role: "host",
    signing_public_key_b64url: await publicB64(hostSign), encryption_public_key_b64url: await publicB64(hostEcdh),
    not_before_unix_ms: Date.now() - 1000, expires_at_unix_ms: Date.now() + 60 * 60 * 1000,
    certificate_id: crypto.randomUUID().replaceAll("-", ""), issuer_device_id: hostId,
  };
  hostCert.signature_b64url = await sign(hostSign.privateKey, certBytes(hostCert));
  const bootstrap = await postRequest({ path: "/v1/enrollment/bootstrap", tenant: TENANT, body: { tenant_id: TENANT, device_name: "Simulated Mac", certificate: hostCert }, bootstrapSecret: "test-bootstrap-only" });
  const response = await relay.fetch(bootstrap, env);
  assert.equal(response.status, 200, await response.clone().text());
  const host = await response.json();
  assert.equal(host.device_id, hostId);
  return { env, hostSign, hostEcdh, hostId, hostCert };
}

async function enrollCompanion(ctx) {
  const companionSign = await keyPair(); const companionEcdh = await ecdhPair();
  const companionId = await deviceIdFromPair(companionSign);
  let req = await postRequest({ path: "/v1/enrollment/invitations", tenant: TENANT, deviceId: ctx.hostId, pair: ctx.hostSign, body: { tenant_id: TENANT, device_role: "companion", expires_in_seconds: 120 } });
  let res = await relay.fetch(req, ctx.env); assert.equal(res.status, 200, await res.clone().text());
  const invitation = await res.json();
  const registration = {
    tenant_id: TENANT,
    invitation_id: invitation.invitation_id,
    invitation_secret_b64url: invitation.invitation_secret_b64url,
    device_name: "Simulated Android",
    device_role: "companion",
    signing_public_key_b64url: await publicB64(companionSign),
    encryption_public_key_b64url: await publicB64(companionEcdh),
    client_nonce_b64url: b64url(crypto.getRandomValues(new Uint8Array(32))),
  };
  req = await new Request("https://relay.test/v1/enrollment/challenge", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(registration) });
  res = await relay.fetch(req, ctx.env); assert.equal(res.status, 200, await res.clone().text());
  const challenge = await res.json();
  Object.assign(registration, { device_id: challenge.device_id, challenge_id: challenge.challenge_id, challenge_b64url: challenge.challenge_b64url });
  const sig = await sign(companionSign.privateKey, enrollmentBytes(registration));
  req = await new Request("https://relay.test/v1/enrollment/complete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...registration, signature_b64url: sig }) });
  res = await relay.fetch(req, ctx.env); assert.equal(res.status, 200, await res.clone().text());
  assert.equal((await res.json()).status, "PENDING_HOST_CERTIFICATION");

  const candidateCert = {
    v: 1, tenant_id: TENANT, device_id: companionId, device_role: "companion",
    signing_public_key_b64url: registration.signing_public_key_b64url,
    encryption_public_key_b64url: registration.encryption_public_key_b64url,
    not_before_unix_ms: Date.now() - 1000, expires_at_unix_ms: Date.now() + 60 * 60 * 1000,
    certificate_id: crypto.randomUUID().replaceAll("-", ""), issuer_device_id: ctx.hostId,
  };
  candidateCert.signature_b64url = await sign(ctx.hostSign.privateKey, certBytes(candidateCert));
  req = await postRequest({ path: "/v1/enrollment/certify", tenant: TENANT, deviceId: ctx.hostId, pair: ctx.hostSign, body: { certificate: candidateCert } });
  res = await relay.fetch(req, ctx.env); assert.equal(res.status, 200, await res.clone().text());
  return { companionSign, companionEcdh, companionId, certificate: candidateCert };
}

test("stale signed requests remain rejected with a distinct retryable timestamp code", async () => {
  const ctx = await setupTenant();
  const path = `/v1/devices/${ctx.hostId}/inbox?limit=5`;
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (line) => warnings.push(JSON.parse(line));
  let stale;
  try {
    stale = await relay.fetch(await postRequest({
      path, method: "GET", deviceId: ctx.hostId, pair: ctx.hostSign, timestampOffsetMs: -60_001,
    }), ctx.env);
  } finally { console.warn = originalWarn; }
  assert.equal(stale.status, 401);
  const staleBody = await stale.json();
  assert.equal(staleBody.error.code, "TIMESTAMP_SKEW");
  assert.match(staleBody.error.request_id, /^[0-9a-f-]{36}$/);
  assert.deepEqual(warnings[0], {
    event: "relay_auth_rejected", request_id: staleBody.error.request_id,
    method: "GET", route: "/v1/devices/:id/inbox", code: "TIMESTAMP_SKEW",
  });

  const unsigned = await relay.fetch(new Request(`https://relay.test${path}`, {
    method: "GET", headers: { "X-Jarvis-Link-Tenant": TENANT },
  }), ctx.env);
  assert.equal(unsigned.status, 401);
  assert.equal((await unsigned.json()).error.code, "UNAUTHORIZED");

  // A fresh, newly signed retry with a new nonce still succeeds. The skew window is not widened.
  const retried = await relay.fetch(await postRequest({
    path, method: "GET", deviceId: ctx.hostId, pair: ctx.hostSign,
  }), ctx.env);
  assert.equal(retried.status, 200, await retried.clone().text());
});

test("host bootstrap, one-use enrollment, encrypted queue, ACK, revoke, and replay controls", async () => {
  const ctx = await setupTenant();
  const companion = await enrollCompanion(ctx);
  const envelope = await makeEnvelope({ tenant: TENANT, from: companion.companionId, to: { id: ctx.hostId, encryption_public_key_b64url: ctx.hostCert.encryption_public_key_b64url }, sender: companion.companionSign, toKeyId: ctx.hostCert.certificate_id });
  const path = "/v1/messages";
  const sendReq = await postRequest({ path, tenant: TENANT, deviceId: companion.companionId, pair: companion.companionSign, body: envelope });
  const sendRes = await relay.fetch(sendReq, ctx.env);
  assert.equal(sendRes.status, 200, await sendRes.clone().text());
  assert.equal((await sendRes.json()).status, "queued");
  const tenantDo = ctx.env.SESSIONS.sessions.get(TENANT);
  const stored = await tenantDo.state.storage.get(`message:${envelope.message_id}`);
  assert.equal(JSON.stringify(stored).includes("safe simulated task"), false, "relay storage must contain ciphertext only");

  // Retrying byte-identical delivery is idempotent even with a fresh request nonce.
  const duplicateSend = await relay.fetch(await postRequest({ path, tenant: TENANT, deviceId: companion.companionId, pair: companion.companionSign, body: envelope }), ctx.env);
  assert.equal(duplicateSend.status, 200);
  assert.equal((await duplicateSend.json()).message_id, envelope.message_id);

  const replayNonce = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const inboxReq = await postRequest({ path: `/v1/devices/${ctx.hostId}/inbox?limit=5`, method: "GET", tenant: TENANT, deviceId: ctx.hostId, pair: ctx.hostSign, nonce: replayNonce });
  const inboxRes = await relay.fetch(inboxReq, ctx.env);
  assert.equal(inboxRes.status, 200, await inboxRes.clone().text());
  const inbox = await inboxRes.json();
  assert.equal(inbox.messages.length, 1);
  assert.equal(inbox.messages[0].envelope.message_id, envelope.message_id);
  assert.deepEqual(inbox.messages[0].sender_certificate, companion.certificate);

  const sameNonceReq = await postRequest({ path: `/v1/devices/${ctx.hostId}/inbox?limit=5`, method: "GET", tenant: TENANT, deviceId: ctx.hostId, pair: ctx.hostSign, nonce: replayNonce });
  const replayAuth = await relay.fetch(sameNonceReq, ctx.env);
  assert.equal(replayAuth.status, 409);
  assert.equal((await replayAuth.json()).error.code, "NONCE_REPLAY");

  const changedEnvelope = { ...envelope, ciphertext_b64url: b64url(crypto.getRandomValues(new Uint8Array(32))) };
  changedEnvelope.sender_signature_b64url = await sign(companion.companionSign.privateKey, te.encode(`${envelopeHeader(changedEnvelope)}\n${await shaHex(fromB64url(changedEnvelope.ciphertext_b64url))}`));
  const changedReq = await postRequest({ path, tenant: TENANT, deviceId: companion.companionId, pair: companion.companionSign, body: changedEnvelope });
  const changedRes = await relay.fetch(changedReq, ctx.env);
  assert.equal(changedRes.status, 409);
  assert.equal((await changedRes.json()).error.code, "MESSAGE_REPLAY");

  const ackBody = { status: "received", result_digest: "a".repeat(64) };
  const ackReq = await postRequest({ path: `/v1/messages/${envelope.message_id}/ack`, tenant: TENANT, deviceId: ctx.hostId, pair: ctx.hostSign, body: ackBody });
  const ackRes = await relay.fetch(ackReq, ctx.env);
  assert.equal(ackRes.status, 200, await ackRes.clone().text());
  assert.equal((await ackRes.json()).status, "acked");
  const duplicateAck = await relay.fetch(await postRequest({ path: `/v1/messages/${envelope.message_id}/ack`, tenant: TENANT, deviceId: ctx.hostId, pair: ctx.hostSign, body: ackBody }), ctx.env);
  assert.equal(duplicateAck.status, 200);
  const conflictingAck = await relay.fetch(await postRequest({ path: `/v1/messages/${envelope.message_id}/ack`, tenant: TENANT, deviceId: ctx.hostId, pair: ctx.hostSign, body: { status: "rejected", result_digest: "b".repeat(64) } }), ctx.env);
  assert.equal(conflictingAck.status, 409);
  assert.equal((await conflictingAck.json()).error.code, "MESSAGE_REPLAY");

  const revokeReq = await postRequest({ path: `/v1/devices/${companion.companionId}/revoke`, tenant: TENANT, deviceId: ctx.hostId, pair: ctx.hostSign, body: { reason_code: "owner_revoked" } });
  assert.equal((await relay.fetch(revokeReq, ctx.env)).status, 200);
  const revokedSend = await relay.fetch(await postRequest({ path, tenant: TENANT, deviceId: companion.companionId, pair: companion.companionSign, body: await makeEnvelope({ tenant: TENANT, from: companion.companionId, to: { id: ctx.hostId, encryption_public_key_b64url: ctx.hostCert.encryption_public_key_b64url }, sender: companion.companionSign, toKeyId: ctx.hostCert.certificate_id }) }), ctx.env);
  assert.equal(revokedSend.status, 401);
  assert.equal((await revokedSend.json()).error.code, "DEVICE_REVOKED");

  const expired = await makeEnvelope({
    tenant: TENANT, from: ctx.hostId,
    to: { id: companion.companionId, encryption_public_key_b64url: companion.certificate.encryption_public_key_b64url },
    sender: ctx.hostSign, toKeyId: companion.certificate.certificate_id,
    issued: Date.now() - 120_000, expires: Date.now() - 1,
  });
  const expiredRes = await relay.fetch(await postRequest({ path, tenant: TENANT, deviceId: ctx.hostId, pair: ctx.hostSign, body: expired }), ctx.env);
  assert.equal(expiredRes.status, 410);
  assert.equal((await expiredRes.json()).error.code, "MESSAGE_EXPIRED");
});

test("typed fast.command is queued as ciphertext for the authenticated host", async () => {
  const ctx = await setupTenant();
  const companion = await enrollCompanion(ctx);
  const envelope = await makeEnvelope({
    tenant: TENANT, from: companion.companionId,
    to: { id: ctx.hostId, encryption_public_key_b64url: ctx.hostCert.encryption_public_key_b64url },
    sender: companion.companionSign, toKeyId: ctx.hostCert.certificate_id,
    kind: "fast.command", text: JSON.stringify({ command: "mac.status", version: 1 }),
  });
  const response = await relay.fetch(await postRequest({
    path: "/v1/messages", tenant: TENANT, deviceId: companion.companionId,
    pair: companion.companionSign, body: envelope,
  }), ctx.env);
  assert.equal(response.status, 200, await response.clone().text());
  const stored = await ctx.env.SESSIONS.sessions.get(TENANT).state.storage.get(`message:${envelope.message_id}`);
  assert.ok(stored?.envelope?.ciphertext_b64url);
  assert.equal(JSON.stringify(stored).includes("mac.status"), false, "relay must not inspect or store command plaintext");
});

test("bootstrap secret, unauthenticated access, and route allowlist fail closed", async () => {
  const ctx = await setupTenant();
  const bootstrapAgain = await relay.fetch(await postRequest({
    path: "/v1/enrollment/bootstrap", tenant: TENANT,
    body: { tenant_id: TENANT, certificate: ctx.hostCert },
    bootstrapSecret: "wrong-bootstrap-secret",
  }), ctx.env);
  assert.equal(bootstrapAgain.status, 401);
  const unsignedInbox = await relay.fetch(new Request(`https://relay.test/v1/devices/${ctx.hostId}/inbox?limit=5`, {
    headers: { "X-Jarvis-Link-Tenant": TENANT },
  }), ctx.env);
  assert.equal(unsignedInbox.status, 401);
  const shellPath = await relay.fetch(new Request("https://relay.test/v1/shell_exec", {
    headers: { "X-Jarvis-Link-Tenant": TENANT },
  }), ctx.env);
  assert.equal(shellPath.status, 404);
  assert.equal((await shellPath.json()).error.code, "NOT_FOUND");
});

test("expiry removes ACKed and pending ciphertext, keeps a bounded tombstone, then purges it", async () => {
  const ctx = await setupTenant();
  const companion = await enrollCompanion(ctx);
  const target = { id: ctx.hostId, encryption_public_key_b64url: ctx.hostCert.encryption_public_key_b64url };
  const send = async (messageId) => {
    const envelope = await makeEnvelope({
      tenant: TENANT, from: companion.companionId, to: target,
      sender: companion.companionSign, toKeyId: ctx.hostCert.certificate_id,
      messageId, text: `secret ${messageId}`,
    });
    const response = await relay.fetch(await postRequest({
      path: "/v1/messages", tenant: TENANT, deviceId: companion.companionId,
      pair: companion.companionSign, body: envelope,
    }), ctx.env);
    assert.equal(response.status, 200, await response.clone().text());
    return envelope;
  };

  const ackedEnvelope = await send(crypto.randomUUID().replaceAll("-", ""));
  const inbox = await relay.fetch(await postRequest({
    path: `/v1/devices/${ctx.hostId}/inbox?limit=5`, method: "GET", tenant: TENANT,
    deviceId: ctx.hostId, pair: ctx.hostSign,
  }), ctx.env);
  assert.equal(inbox.status, 200);
  const ackedBody = { status: "received", result_digest: "a".repeat(64) };
  const ack = await relay.fetch(await postRequest({
    path: `/v1/messages/${ackedEnvelope.message_id}/ack`, tenant: TENANT,
    deviceId: ctx.hostId, pair: ctx.hostSign, body: ackedBody,
  }), ctx.env);
  assert.equal(ack.status, 200);

  const pendingEnvelope = await send(crypto.randomUUID().replaceAll("-", ""));
  const tenantDo = ctx.env.SESSIONS.sessions.get(TENANT);
  const expiryNow = Date.now() - 1;
  for (const id of [ackedEnvelope.message_id, pendingEnvelope.message_id]) {
    const record = await tenantDo.state.storage.get(`message:${id}`);
    record.envelope.expires_at_unix_ms = expiryNow;
    await tenantDo.state.storage.put(`message:${id}`, record);
  }

  await tenantDo.alarm();
  const ackedTombstone = await tenantDo.state.storage.get(`message:${ackedEnvelope.message_id}`);
  const expiredTombstone = await tenantDo.state.storage.get(`message:${pendingEnvelope.message_id}`);
  assert.equal(ackedTombstone.status, "ACKED");
  assert.equal(expiredTombstone.status, "EXPIRED");
  assert.equal(ackedTombstone.envelope, null);
  assert.equal(expiredTombstone.envelope, null);
  assert.ok(ackedTombstone.purge_at_unix_ms > Date.now());
  assert.ok(expiredTombstone.purge_at_unix_ms > Date.now());
  assert.equal(await tenantDo.state.storage.get(`inbox:${ctx.hostId}`), undefined);

  for (const id of [ackedEnvelope.message_id, pendingEnvelope.message_id]) {
    const record = await tenantDo.state.storage.get(`message:${id}`);
    record.purge_at_unix_ms = Date.now() - 1;
    await tenantDo.state.storage.put(`message:${id}`, record);
  }
  await tenantDo.alarm();
  assert.equal(await tenantDo.state.storage.get(`message:${ackedEnvelope.message_id}`), undefined);
  assert.equal(await tenantDo.state.storage.get(`message:${pendingEnvelope.message_id}`), undefined);

  // Older releases left already-cleared EXPIRED tombstones without a purge
  // deadline. A later scheduling pass migrates them and cleans stale queue IDs.
  const legacyId = "d".repeat(32);
  await tenantDo.state.storage.put(`message:${legacyId}`, {
    to_device_id: ctx.hostId, status: "EXPIRED", envelope: null,
  });
  await tenantDo.state.storage.put(`inbox:${ctx.hostId}`, [legacyId]);
  await tenantDo.scheduleExpiry();
  const migrated = await tenantDo.state.storage.get(`message:${legacyId}`);
  assert.ok(migrated.purge_at_unix_ms > Date.now());
  assert.equal(await tenantDo.state.storage.get(`inbox:${ctx.hostId}`), undefined);
  migrated.purge_at_unix_ms = Date.now() - 1;
  await tenantDo.state.storage.put(`message:${legacyId}`, migrated);
  await tenantDo.alarm();
  assert.equal(await tenantDo.state.storage.get(`message:${legacyId}`), undefined);
});

test("request nonce and rate-limit state are tenant-local and expire from storage", async () => {
  const ctx = await setupTenant();
  const response = await relay.fetch(await postRequest({
    path: `/v1/devices/${ctx.hostId}/certificate`, method: "GET", tenant: TENANT,
    deviceId: ctx.hostId, pair: ctx.hostSign,
  }), ctx.env);
  assert.equal(response.status, 200);

  const tenantDo = ctx.env.SESSIONS.sessions.get(TENANT);
  let nonceRows = await tenantDo.state.storage.list({ prefix: "nonce:" });
  let rateRows = await tenantDo.state.storage.list({ prefix: "rate:" });
  assert.equal(nonceRows.size, 1);
  assert.equal(rateRows.size, 1);
  assert.ok(tenantDo.state.storage.alarm > Date.now());

  const [nonceKey] = nonceRows.keys();
  await tenantDo.state.storage.put(nonceKey, Date.now() - 1);
  const previousWindow = Math.floor(Date.now() / 60_000) - 2;
  const oldRateKey = `rate:${ctx.hostId}:${previousWindow}`;
  await tenantDo.state.storage.put(oldRateKey, 120);
  await tenantDo.alarm();
  assert.equal(await tenantDo.state.storage.get(nonceKey), undefined);
  assert.equal(await tenantDo.state.storage.get(oldRateKey), undefined);
  assert.equal((await tenantDo.state.storage.list({ prefix: "rate:" })).size, 1);
});

test("expired enrollment challenges and invitations are purged by the tenant alarm", async () => {
  const ctx = await setupTenant();
  await enrollCompanion(ctx);
  const tenantDo = ctx.env.SESSIONS.sessions.get(TENANT);
  const [challengeEntry] = [...(await tenantDo.state.storage.list({ prefix: "enroll:" })).entries()];
  assert.ok(challengeEntry);
  const [challengeKey, challenge] = challengeEntry;
  const invitationKey = `invite:${challenge.invitation_id}`;
  const invitation = await tenantDo.state.storage.get(invitationKey);
  assert.ok(invitation);
  challenge.expires_at_unix_ms = Date.now() - 1;
  invitation.expires_at_unix_ms = Date.now() - 1;
  await tenantDo.state.storage.put(challengeKey, challenge);
  await tenantDo.state.storage.put(invitationKey, invitation);

  await tenantDo.alarm();
  assert.equal(await tenantDo.state.storage.get(challengeKey), undefined);
  assert.equal(await tenantDo.state.storage.get(invitationKey), undefined);
});

test("control inbox reaches signed decisions behind a full task page without granting cross-device access", async () => {
  const ctx = await setupTenant();
  const companion = await enrollCompanion(ctx);
  const args = { tenant: TENANT, from: companion.companionId,
    to: { id: ctx.hostId, encryption_public_key_b64url: ctx.hostCert.encryption_public_key_b64url },
    sender: companion.companionSign, toKeyId: ctx.hostCert.certificate_id };
  for (let i = 0; i < 51; i++) {
    const item = await makeEnvelope(args);
    const sent = await relay.fetch(await postRequest({path: "/v1/messages", tenant: TENANT,
      deviceId: companion.companionId, pair: companion.companionSign, body: item}), ctx.env);
    assert.equal(sent.status, 200);
  }
  const control = await makeEnvelope({...args, kind: "task.cancel"});
  assert.equal((await relay.fetch(await postRequest({path: "/v1/messages", tenant: TENANT,
    deviceId: companion.companionId, pair: companion.companionSign, body: control}), ctx.env)).status, 200);
  const path = `/v1/devices/${ctx.hostId}/inbox?limit=50&filter=control`;
  const unauthorized = await relay.fetch(await postRequest({method: "GET", path, tenant: TENANT,
    deviceId: companion.companionId, pair: companion.companionSign}), ctx.env);
  assert.equal(unauthorized.status, 403);
  const response = await relay.fetch(await postRequest({method: "GET", path, tenant: TENANT,
    deviceId: ctx.hostId, pair: ctx.hostSign}), ctx.env);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.messages.length, 1);
  assert.equal(body.messages[0].envelope.message_id, control.message_id);
  const fifo = `/v1/devices/${ctx.hostId}/inbox?limit=50`;
  const normal = await relay.fetch(await postRequest({method: "GET", path: fifo, tenant: TENANT,
    deviceId: ctx.hostId, pair: ctx.hostSign}), ctx.env);
  assert.equal((await normal.json()).messages.length, 50);
});
