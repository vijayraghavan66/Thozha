/**
 * Versioned browser API types for Thozha Web.
 *
 * These types describe implemented, auth-gated session/conversation/memory
 * operations only. Supervisor task, approval, and device types are added when
 * their backend routes land; do not infer endpoint support from UI mocks.
 */

export interface WebLoginRequest {
  username: string;
  password: string;
}

export interface WebSession {
  user_id: string;
  /** Unix epoch seconds. */
  expires_at: number;
  csrf_token: string;
}

export interface WebConversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export type WebMessageStatus =
  | "accepted"
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface WebMessage {
  id: string;
  conversation_id: string;
  role: "user" | "assistant";
  content: string;
  status: WebMessageStatus;
  response_to: string | null;
  error_code: string | null;
  created_at: string;
  updated_at: string;
}

export interface WebConversationEvent {
  sequence: number;
  message_id: string;
  event_type:
    | "accepted"
    | "queued"
    | "started"
    | "completed"
    | "failed"
    | "cancelled"
    | "cancellation_requested";
  created_at: string;
  details: Record<string, unknown>;
}

export interface WebConversationDetail extends WebConversation {
  messages: WebMessage[];
  events: WebConversationEvent[];
}

export interface WebMessageAccepted {
  conversation_id: string;
  message_id: string;
  response_id: string;
  status: WebMessageStatus;
  replayed: boolean;
}

export type WebConversationStreamMode = "engine_stream" | "buffered";

export type WebConversationSseEvent =
  | {
      event: "accepted";
      data: Omit<WebMessageAccepted, "message_id"> & { message_id: string };
    }
  | { event: "started"; data: { response_id: string } }
  | {
      event: "stream_mode";
      data: { response_id: string; mode: WebConversationStreamMode };
    }
  | { event: "delta"; data: { response_id: string; content: string } }
  | { event: "message"; data: { response_id: string; content: string } }
  | {
      event: "done";
      data: {
        response_id: string;
        status: WebMessageStatus;
        replayed?: boolean;
        stream_mode?: WebConversationStreamMode;
      };
    }
  | { event: "error"; data: { response_id: string; code: "generation_failed" } };

export interface WebMemory {
  id: string;
  title: string;
  content: string;
  tags: string[];
  created_at: string;
  updated_at: string;
}

export interface WebMemoryCreate {
  content: string;
  title?: string | null;
  tags?: string[];
}

export interface WebMemoryCreateResult {
  memory: WebMemory;
  replayed: boolean;
}

export interface WebMemoryList {
  items: WebMemory[];
  search_mode: "substring";
}

export const WEB_API_V1 = {
  session: "/v1/web/session",
  login: "/v1/web/session/login",
  logout: "/v1/web/session/logout",
  conversations: "/v1/web/conversations",
  memory: "/v1/web/memory",
} as const;
