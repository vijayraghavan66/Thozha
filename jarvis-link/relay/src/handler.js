const TENANT_RE = /^[a-zA-Z0-9_-]{8,64}$/;
const MAX_BODY_BYTES = 64 * 1024;

function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers },
  });
}

function error(code, status, message) {
  return json({ error: { code, message, request_id: crypto.randomUUID() } }, status);
}

async function readTenant(request) {
  const headerTenant = request.headers.get("X-Jarvis-Link-Tenant");
  if (headerTenant) return headerTenant;
  if (request.method === "POST") {
    const length = Number(request.headers.get("content-length") || 0);
    if (length > MAX_BODY_BYTES) throw Object.assign(new Error("too_large"), { status: 413 });
    const clone = request.clone();
    const bytes = new Uint8Array(await clone.arrayBuffer());
    if (bytes.length > MAX_BODY_BYTES) throw Object.assign(new Error("too_large"), { status: 413 });
    try {
      const body = JSON.parse(new TextDecoder().decode(bytes));
      return body.tenant_id || null;
    } catch {
      return null;
    }
  }
  return null;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health" && request.method === "GET") return json({ status: "ok", protocol: "jarvis-link-v1" });

    if (url.pathname === "/v1/enrollment/bootstrap" && request.method === "POST") {
      const expected = env.JARVIS_LINK_BOOTSTRAP_SECRET;
      const supplied = request.headers.get("X-Jarvis-Link-Bootstrap-Key") || "";
      if (!expected || !supplied || !constantTimeEqual(supplied, expected)) {
        return error("UNAUTHORIZED", 401, "A valid one-time host bootstrap authorization is required");
      }
    }

    let tenant;
    try {
      tenant = await readTenant(request);
    } catch (exc) {
      if (exc?.status === 413) return error("INVALID_ENVELOPE", 413, "Request body exceeds the relay limit");
      return error("INVALID_ENVELOPE", 400, "Malformed request body");
    }
    if (!tenant || !TENANT_RE.test(tenant)) return error("INVALID_ENVELOPE", 400, "A valid tenant ID is required");
    const id = env.SESSIONS.idFromName(tenant);
    const stub = env.SESSIONS.get(id);
    const headers = new Headers(request.headers);
    headers.set("X-Jarvis-Link-Resolved-Tenant", tenant);
    const upstream = new Request(request, { headers, redirect: "manual" });
    return stub.fetch(upstream);
  },
};

function constantTimeEqual(left, right) {
  const a = new TextEncoder().encode(left);
  const b = new TextEncoder().encode(right);
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}


