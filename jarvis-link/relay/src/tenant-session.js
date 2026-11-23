import { DurableObject } from "cloudflare:workers";
import { TenantSessionCore } from "./tenant-session-core.js";

/** Cloudflare Durable Object adapter. Stateful protocol logic is in the shared core. */
export class TenantSession extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.core = new TenantSessionCore(ctx, env);
  }
  fetch(request) { return this.core.fetch(request); }
  alarm() { return this.core.alarm(); }
  webSocketMessage(socket, message) { return this.core.webSocketMessage(socket, message); }
  webSocketClose(socket, code, reason, wasClean) { return this.core.webSocketClose(socket, code, reason, wasClean); }
  webSocketError(socket, error) { return this.core.webSocketError(socket, error); }
}
