#!/usr/bin/env node
/**
 * Offline authenticated relay scale simulation.
 *
 * Creates synthetic tenant/device state in memory and calls the production
 * TenantSessionCore handler directly. It performs no HTTP listener, Cloudflare
 * request, Telegram operation, database write, or model request. Registration
 * is seeded (the invitation/certification flow is covered by relay.test.js).
 */
import { webcrypto } from "node:crypto";
import { performance } from "node:perf_hooks";
import relay from "../src/handler.js";
import { TenantSessionCore } from "../src/tenant-session-core.js";

const crypto = webcrypto;
const encoder = new TextEncoder();

class MemoryStorage {
  constructor() { this.values = new Map(); this.alarm = null; }
  async get(key) { return this.values.get(key); }
  async put(key, value) { this.values.set(key, structuredClone(value)); }
  async delete(key) { return this.values.delete(key); }
  async list({ prefix = "" } = {}) {
    return new Map([...this.values.entries()].filter(([key]) => key.startsWith(prefix)));
  }
  async setAlarm(value) { this.alarm = value; }
  async getAlarm() { return this.alarm; }
  async deleteAlarm() { this.alarm = null; }
}

class FakeState {
  constructor() { this.storage = new MemoryStorage(); }
  getWebSockets() { return []; }
}

function makeNamespace(env) {
  const sessions = new Map();
  return {
    idFromName(tenant) { return tenant; },
    get(tenant) {
      if (!sessions.has(tenant)) sessions.set(tenant, new TenantSessionCore(new FakeState(), env));
      return sessions.get(tenant);
    },
    sessions,
  };
}

function b64url(bytes) { return Buffer.from(bytes).toString("base64url"); }

function rawToDer(raw) {
  let r = Buffer.from(raw.slice(0, 32));
  let s = Buffer.from(raw.slice(32));
  while (r.length > 1 && r[0] === 0 && !(r[1] & 0x80)) r = r.subarray(1);
  while (s.length > 1 && s[0] === 0 && !(s[1] & 0x80)) s = s.subarray(1);
  if (r[0] & 0x80) r = Buffer.concat([Buffer.from([0]), r]);
  if (s[0] & 0x80) s = Buffer.concat([Buffer.from([0]), s]);
  const content = Buffer.concat([Buffer.from([2, r.length]), r, Buffer.from([2, s.length]), s]);
  return new Uint8Array(Buffer.concat([Buffer.from([0x30, content.length]), content]));
}

async function sha256(bytes) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
}

async function makeIdentity(index, scenario) {
  const pair = await crypto.subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"],
  );
  const publicDer = new Uint8Array(await crypto.subtle.exportKey("spki", pair.publicKey));
  const id = Buffer.from((await sha256(publicDer)).slice(0, 16)).toString("hex");
  const tenant = `scale-${scenario}-${String(index).padStart(5, "0")}`;
  return { tenant, id, pair, publicKey: b64url(publicDer) };
}

async function signedInboxRequest(identity) {
  const path = `/v1/devices/${identity.id}/inbox?limit=5`;
  const timestamp = String(Date.now());
  const nonce = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const bodyHash = Buffer.from(await sha256(new Uint8Array())).toString("hex");
  const signed = `JARVIS-LINK-REQUEST-V1\n${identity.tenant}\n${identity.id}\n${timestamp}\n${nonce}\nGET\n${path}\n${bodyHash}`;
  const signature = b64url(rawToDer(new Uint8Array(await crypto.subtle.sign(
    { name: "ECDSA", hash: "SHA-256" }, identity.pair.privateKey, encoder.encode(signed),
  ))));
  return new Request(`https://relay-simulation.invalid${path}`, {
    method: "GET",
    headers: {
      "X-Jarvis-Link-Tenant": identity.tenant,
      "X-Jarvis-Link-Device": identity.id,
      "X-Jarvis-Link-Timestamp": timestamp,
      "X-Jarvis-Link-Nonce": nonce,
      "X-Jarvis-Link-Signature": signature,
    },
  });
}

function percentile(values, ratio) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * ratio) - 1)] ?? 0;
}

async function mapLimit(items, limit, callback) {
  const result = new Array(items.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (true) {
      const index = next++;
      if (index >= items.length) return;
      result[index] = await callback(items[index], index);
    }
  }));
  return result;
}

async function runConcurrent(items, concurrency, env) {
  const latencies = [];
  const statuses = {};
  const overallStart = performance.now();
  for (let start = 0; start < items.length; start += concurrency) {
    const group = items.slice(start, start + concurrency);
    await Promise.all(group.map(async ({ identity, request }) => {
      const began = performance.now();
      const response = await relay.fetch(request, env);
      latencies.push(performance.now() - began);
      statuses[response.status] = (statuses[response.status] ?? 0) + 1;
      await response.body?.cancel();
    }));
  }
  const elapsed = performance.now() - overallStart;
  return {
    requests: items.length,
    concurrency,
    elapsed_ms: Number(elapsed.toFixed(2)),
    requests_per_second: Number((items.length / (elapsed / 1000)).toFixed(2)),
    p50_ms: Number(percentile(latencies, 0.50).toFixed(2)),
    p95_ms: Number(percentile(latencies, 0.95).toFixed(2)),
    p99_ms: Number(percentile(latencies, 0.99).toFixed(2)),
    statuses,
  };
}

async function main() {
  const env = { SESSIONS: null };
  env.SESSIONS = makeNamespace(env);
  const cpuStart = process.cpuUsage();
  const memoryStart = process.memoryUsage();
  const start = performance.now();
  const cases = [
    { users: 100, concurrency: 50, scenario: "u100" },
    { users: 500, concurrency: 100, scenario: "u500" },
    { users: 1000, concurrency: 250, scenario: "u1000" },
  ];
  const results = [];
  const identities = [];

  for (const item of cases) {
    const tenants = await mapLimit(
      Array.from({ length: item.users }, (_, index) => index),
      16,
      index => makeIdentity(index, item.scenario),
    );
    for (const identity of tenants) {
      const object = env.SESSIONS.get(identity.tenant);
      await object.state.storage.put(`device:${identity.id}`, {
        id: identity.id,
        tenant_id: identity.tenant,
        device_role: "host",
        active: true,
        root: true,
        revoked_at: null,
        signing_public_key_b64url: identity.publicKey,
      });
    }
    const requests = await mapLimit(tenants, 32, async identity => ({
      identity,
      request: await signedInboxRequest(identity),
    }));
    results.push(await runConcurrent(requests, item.concurrency, env));
    identities.push(...tenants);
  }

  const ownerA = identities[0];
  const ownerB = identities[100];
  const original = await signedInboxRequest(ownerA);
  const first = await relay.fetch(original, env);
  const firstStatus = first.status;
  await first.body?.cancel();
  const changedHeaders = new Headers(original.headers);
  changedHeaders.set("X-Jarvis-Link-Tenant", ownerB.tenant);
  const crossTenant = await relay.fetch(new Request(original, { headers: changedHeaders }), env);
  const crossTenantStatus = crossTenant.status;
  await crossTenant.body?.cancel();

  const rateTenant = await makeIdentity(0, "rate-limit");
  const rateObject = env.SESSIONS.get(rateTenant.tenant);
  await rateObject.state.storage.put(`device:${rateTenant.id}`, {
    id: rateTenant.id,
    tenant_id: rateTenant.tenant,
    device_role: "host",
    active: true,
    root: true,
    revoked_at: null,
    signing_public_key_b64url: rateTenant.publicKey,
  });
  const originalDateNow = Date.now;
  const rateWindowNow = Date.now();
  let rateRequests;
  let rateResults;
  try {
    Date.now = () => rateWindowNow;
    rateRequests = await mapLimit(
      Array.from({ length: 121 }, () => rateTenant),
      32,
      async identity => ({ identity, request: await signedInboxRequest(identity) }),
    );
    rateResults = await runConcurrent(rateRequests, 50, env);
  } finally {
    Date.now = originalDateNow;
  }

  const cpu = process.cpuUsage(cpuStart);
  const memoryEnd = process.memoryUsage();
  const elapsedTotal = performance.now() - start;
  console.log(JSON.stringify({
    mode: "in_process_fake_durable_objects_no_network_no_telegram_no_model",
    registered_synthetic_tenants: identities.length,
    scenarios: results,
    cross_tenant_signed_request: { first_status: firstStatus, mismatched_tenant_status: crossTenantStatus },
    per_device_rate_limit: rateResults,
    total_elapsed_ms: Number(elapsedTotal.toFixed(2)),
    cpu_ms: Number(((cpu.user + cpu.system) / 1000).toFixed(2)),
    rss_delta_bytes: memoryEnd.rss - memoryStart.rss,
    heap_used_delta_bytes: memoryEnd.heapUsed - memoryStart.heapUsed,
    note: "Authentication uses generated P-256 identities and seeded in-memory tenant/device rows. This does not measure Cloudflare, durable disk I/O, Telegram, AI, or production capacity.",
  }));
}

main().catch(error => {
  console.error(JSON.stringify({
    error_type: error?.name ?? "Error",
    message: String(error?.message ?? "simulation failed").slice(0, 180),
    stack: String(error?.stack ?? "").split("\n").slice(0, 5),
  }));
  process.exitCode = 1;
});
