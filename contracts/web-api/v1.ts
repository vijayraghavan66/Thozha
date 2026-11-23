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

export type WebTaskScope = "read_only" | "assistant" | "development";

export type WebTaskState =
  | "CREATED"
  | "PLANNING"
  | "RUNNING"
  | "VERIFYING"
  | "WAITING_APPROVAL"
  | "PAUSED"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

export interface WebTaskCreate {
  objective: string;
  capability_scope: [WebTaskScope];
  idempotency_key: string;
  requester_device_id: string;
}

export interface WebTaskResult {
  task_id: string;
  state: WebTaskState;
  summary: string;
  result_text: string | null;
  result_digest: string | null;
  idempotent_replay: boolean;
  error_code: string | null;
}

export interface WebTaskEvent {
  seq: number;
  event_type: string;
  created_at: string;
  payload: Record<string, unknown>;
}

export interface WebTaskEventPage {
  task_id: string;
  events: WebTaskEvent[];
  next_seq: number;
}

export interface WebApproval {
  id: string;
  task_id: string;
  operation: string;
  target_resource: string;
  canonical_arguments: Record<string, unknown>;
  operation_digest: string;
  reason: string;
  status: string;
  created_at: string;
  expires_at: string;
}

export interface WebApprovalDecision {
  decision: "approve" | "reject";
  task_id?: string | null;
  expected_operation_digest: string;
  idempotency_key: string;
  requester_device_id: string;
  continuation_objective?: string | null;
}

export interface WebDevice {
  device_id: string;
  device_name: string;
  created_at: string;
  expires_at: string;
  status: "active";
}

export interface WebDeviceList {
  devices: WebDevice[];
  count: number;
}

export interface WebDeviceRevokeResult {
  status: "revoked" | "already_revoked";
  device_id: string;
  notification_token_cleanup: "none" | "revoked";
}

export interface WebArtifact {
  artifact_id: string;
  task_id: string;
  file_name: string;
  size_bytes: number;
  archive_sha256: string;
  manifest_sha256: string;
  files: Array<Record<string, unknown>>;
  download_path: string;
}

export type WebAutomationSchedule = "once" | "interval" | "cron";
export type WebAutomationStatus = "active" | "paused" | "completed" | "cancelled";

export interface WebAutomationCreate {
  prompt: string;
  schedule_type: WebAutomationSchedule;
  schedule_value: string;
  timezone?: string | null;
  device_id: string;
}

export interface WebAutomationUpdate {
  prompt?: string;
  schedule_type?: WebAutomationSchedule;
  schedule_value?: string;
  timezone?: string;
}

export interface WebAutomation {
  id: string;
  prompt: string;
  schedule_type: WebAutomationSchedule;
  schedule_value: string;
  timezone: string | null;
  status: WebAutomationStatus;
  next_run: string | null;
  last_run: string | null;
  last_supervisor_task_id: string | null;
}

export interface WebAutomationSupervisorTask {
  task_id: string;
  state: WebTaskState;
  link_state: string | null;
  result_summary: string | null;
  result_text: string | null;
  result_truncated: boolean;
  events: Array<{ seq: number; type: string; created_at: string; state: string | null }>;
}

export interface WebAutomationRun {
  started_at: string;
  finished_at: string | null;
  success: boolean;
  state: string;
  supervisor_task_id: string | null;
  idempotency_key: string | null;
  result: string;
  error: string;
  supervisor_task: WebAutomationSupervisorTask | null;
}

export interface WebAutomationCapabilities {
  status: "available";
  operations: Record<string, boolean>;
  unavailable_reasons: Record<string, string>;
}

export const WEB_API_V1 = {
  session: "/v1/web/session",
  login: "/v1/web/session/login",
  logout: "/v1/web/session/logout",
  conversations: "/v1/web/conversations",
  memory: "/v1/web/memory",
  tasks: "/v1/web/tasks",
  approvals: "/v1/web/approvals",
  devices: "/v1/web/devices",
  automations: "/v1/web/automations",
} as const;
