/**
 * Shared TypeScript contract additions for the authenticated Thozha Web API v1.
 * OpenAPI remains canonical; these narrow types describe the 2026-11-24 delta.
 */

export interface WebConversationRenameRequest {
  /** Trimmed, nonblank, at most 160 UTF-8 bytes on the server. */
  title: string;
}

export interface WebConversationRenameResponse {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export interface WebConversationCompletedEventDetails {
  /** Number of explicitly opted-in, relevant memories included in model context. */
  memory_context_count: number;
  /** Opaque owner-scoped memory IDs; memory text is never included in event details. */
  memory_ids: string[];
  error_code?: string;
}

/** Memory tags opt in through `chat_context`; exclusion tags always take precedence. */
export type WebMemoryContextOptInTag = 'chat_context';
export type WebMemoryContextExclusionTag =
  | 'sensitive'
  | 'private'
  | 'no_chat'
  | 'never_inject';
