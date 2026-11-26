// Generated from openapi/thozha-v1.yaml at supplied OpenAPI file.
// Do not edit. Run npm run contract:generate.

export interface paths {
    "/v1/web/session/login": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** @description Implemented but disabled unless THOZHA_WEB_OWNER_ID, THOZHA_WEB_PASSWORD_HASH, and THOZHA_WEB_ORIGIN are configured outside source control. Requires an exact same-origin Origin header. Sets an opaque HttpOnly SameSite=Strict cookie; never accepts Android tokens. */
        post: operations["createWebSession"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/session": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getWebSession"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/session/logout": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** @description Requires exact same-origin Origin, application/json, and X-Thozha-CSRF. */
        post: operations["revokeWebSession"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/conversations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listWebConversations"];
        put?: never;
        /** @description Owner identity is derived from the server-side browser session. */
        post: operations["createWebConversation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/conversations/{conversation_id}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
            };
            cookie?: never;
        };
        get: operations["getWebConversation"];
        put?: never;
        post?: never;
        /** @description Requires exact same-origin Origin, application/json, and X-Thozha-CSRF. */
        delete: operations["deleteWebConversation"];
        options?: never;
        head?: never;
        /** @description Requires exact same-origin Origin, application/json, and X-Thozha-CSRF. */
        patch: operations["renameWebConversation"];
        trace?: never;
    };
    "/v1/web/conversations/{conversation_id}/archive": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** @description Requires exact same-origin Origin, application/json, and X-Thozha-CSRF. */
        post: operations["archiveWebConversation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/conversations/{conversation_id}/attachments": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** @description Uploads a raw PNG, JPEG, or PDF body (8 MiB/file; ten files and 30 MiB per conversation; 100 MiB per owner). The file is stored in the owner-scoped conversation database. macOS Vision/PDFKit extracts text locally; only extracted, bounded text is added to model context when the message references the returned attachment ID. This is OCR/text extraction, not image-native visual reasoning. Attachment content is treated as untrusted data and is never used to enable tools. */
        post: operations["uploadWebConversationAttachment"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/conversations/{conversation_id}/attachments/{attachment_id}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
                attachment_id: string;
            };
            cookie?: never;
        };
        get: operations["downloadWebConversationAttachment"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/conversations/{conversation_id}/messages": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** @description Persists the message and calls the configured model with tools disabled. The response is asynchronous (202) but this is not a streaming API. A blocking model call cannot be forcibly cancelled; late output is suppressed. */
        post: operations["submitWebConversationMessage"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/conversations/{conversation_id}/messages/{message_id}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
                message_id: string;
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** @description message_id is the assistant response ID; cancellation suppresses late results but may not stop inference already running. */
        post: operations["cancelWebConversationMessage"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/conversations/{conversation_id}/messages/stream": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** @description Uses the configured engine's actual stream() method with tools disabled. Engines with native streaming emit deltas incrementally. Codex SDK emits one buffered result after generate() completes and is labeled buffered. Disconnect/cancel suppresses late result persistence, but blocking inference cannot be forcibly interrupted. */
        post: operations["streamWebConversationMessage"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/memory": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listWebMemory"];
        put?: never;
        /** @description Owner-scoped persistent memory. Requires exact same-origin Origin, JSON, CSRF, and Idempotency-Key. Owner limits are 5000 entries and 20 MiB content. */
        post: operations["createWebMemory"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/memory/search": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** @description Deterministic case-insensitive substring search; not semantic retrieval. */
        get: operations["searchWebMemory"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/memory/{memory_id}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                memory_id: string;
            };
            cookie?: never;
        };
        get: operations["getWebMemory"];
        put?: never;
        post?: never;
        /** @description Requires exact same-origin Origin, application/json, and X-Thozha-CSRF. */
        delete: operations["deleteWebMemory"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/supervisor/readiness": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** @description Returns only safe component states and reason codes. Codex credential file presence is preflight evidence and is not reported as runtime authentication unless a model turn has succeeded in this process. */
        get: operations["getWebSupervisorReadiness"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/tasks": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listWebSupervisorTasks"];
        put?: never;
        /** @description Creates a durable Link task owned by an explicitly mapped paired device. Only existing Link scopes are accepted. Execution uses the existing Link runner; approvals and sandbox restrictions are unchanged. */
        post: operations["submitWebSupervisorTask"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/tasks/{task_id}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_id: components["parameters"]["TaskId"];
            };
            cookie?: never;
        };
        get: operations["getWebSupervisorTask"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/tasks/{task_id}/events": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_id: components["parameters"]["TaskId"];
            };
            cookie?: never;
        };
        get: operations["getWebSupervisorTaskEvents"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/tasks/{task_id}/events/stream": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_id: components["parameters"]["TaskId"];
            };
            cookie?: never;
        };
        /** @description Bounded replayable SSE of persisted task events; not model-token streaming. */
        get: operations["streamWebSupervisorTaskEvents"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/tasks/{task_id}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_id: components["parameters"]["TaskId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["cancelWebSupervisorTask"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/approvals/pending": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listWebPendingApprovals"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/approvals/{approval_id}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                approval_id: components["parameters"]["ApprovalId"];
            };
            cookie?: never;
        };
        get: operations["getWebApproval"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/approvals/{approval_id}/decision": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                approval_id: components["parameters"]["ApprovalId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** @description Reuses the Android exact-digest, expiry, replay, dispatch-claim and executor path. */
        post: operations["decideWebApproval"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/tasks/{task_id}/artifacts": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_id: components["parameters"]["TaskId"];
            };
            cookie?: never;
        };
        get: operations["listWebTaskArtifacts"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/tasks/{task_id}/artifacts/{artifact_id}/download": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_id: components["parameters"]["TaskId"];
                artifact_id: string;
            };
            cookie?: never;
        };
        get: operations["downloadWebTaskArtifact"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/devices": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listWebDevices"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/devices/pairing-invitations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** @description Creates a five-minute signed Wi-Fi QR invitation for the authenticated owner of this Mac installation. The endpoint fails closed unless the configured private Wi-Fi address, SSID, pinned gateway certificate, and gateway listener are verified. It never creates a loopback-only QR through the Web UI and does not add the resulting phone to the separate THOZHA_WEB_ALLOWED_DEVICE_IDS task-actor mapping. */
        post: operations["createWebPairingInvitation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/devices/pairing-requests": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listWebPairingRequests"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/devices/pairing-requests/{invitation_id}/decision": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** @description Records the authenticated Mac installation owner's explicit decision. Approve permits only the invitation-scoped phone claim; it does not itself issue a device credential. After the phone completes its invitation-bound key proof and claims its credential, the server creates an owner-scoped Web device mapping for this approving Web owner. The mapping is not active before claim. Reject never creates a paired device or Web mapping. */
        post: operations["decideWebPairingRequest"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/devices/pairing-invitations/{invitation_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** @description Returns invitation lifecycle only; never returns the phone's claim challenge or device token. */
        get: operations["getWebPairingInvitationStatus"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/devices/pairing-invitations/{invitation_id}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["cancelWebPairingInvitation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/devices/{device_id}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                device_id: components["parameters"]["WebDeviceId"];
            };
            cookie?: never;
        };
        get: operations["getWebDevice"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/devices/{device_id}/revoke": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                device_id: components["parameters"]["WebDeviceId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** @description Confirmed revocation only; no pairing/enrollment operation is exposed. */
        post: operations["revokeWebDevice"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/automations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listWebAutomations"];
        put?: never;
        /** @description Read-only scheduled requests only. Each occurrence is a durable Link task bound to the selected active paired device and uses the ordinary read_only supervisor scope. No arbitrary tools are scheduled. */
        post: operations["createWebAutomation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/automations/capabilities": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getWebAutomationCapabilities"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/automations/{automation_id}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                automation_id: string;
            };
            cookie?: never;
        };
        get: operations["getWebAutomation"];
        put?: never;
        post?: never;
        /** @description Soft-cancels an owned schedule; history is retained. */
        delete: operations["deleteWebAutomation"];
        options?: never;
        head?: never;
        /** @description Update only an owned Web schedule; supported scope remains read_only. */
        patch: operations["updateWebAutomation"];
        trace?: never;
    };
    "/v1/web/automations/{automation_id}/history": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                automation_id: string;
            };
            cookie?: never;
        };
        get: operations["getWebAutomationHistory"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/automations/{automation_id}/enable": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                automation_id: string;
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["enableWebAutomation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/automations/{automation_id}/disable": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                automation_id: string;
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["disableWebAutomation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/automations/{automation_id}/pause": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                automation_id: string;
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["pauseWebAutomation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/web/automations/{automation_id}/trigger": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                automation_id: string;
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** @description Runs one read_only occurrence through a durable supervisor Link task. */
        post: operations["triggerWebAutomation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        WebLoginRequest: {
            username: string;
            password: string;
        };
        WebSession: {
            user_id: string;
            /** Format: int64 */
            expires_at: number;
            csrf_token: string;
        };
        Error: {
            detail: string | {
                [key: string]: unknown;
            } | unknown[];
        };
        WebConversationCreate: {
            title?: string | null;
        };
        WebConversation: {
            /** Format: uuid */
            id?: string;
            title?: string;
            /** Format: date-time */
            created_at?: string;
            /** Format: date-time */
            updated_at?: string;
            /** Format: date-time */
            archived_at?: string | null;
        };
        WebConversationDetail: components["schemas"]["WebConversation"] & {
            messages?: components["schemas"]["WebMessage"][];
            events?: components["schemas"]["WebConversationEvent"][];
        };
        WebMessage: {
            /** Format: uuid */
            id?: string;
            /** Format: uuid */
            conversation_id?: string;
            /** @enum {string} */
            role?: "user" | "assistant";
            content?: string;
            /** @enum {string} */
            status?: "accepted" | "queued" | "running" | "completed" | "failed" | "cancelled";
            /** Format: uuid */
            response_to?: string | null;
            error_code?: string | null;
            /** Format: date-time */
            created_at?: string;
            /** Format: date-time */
            updated_at?: string;
            attachment_ids?: string[];
            attachments?: components["schemas"]["WebAttachmentMetadata"][];
        };
        WebAttachmentMetadata: {
            id: string;
            file_name: string;
            /** @enum {string} */
            media_type: "image/png" | "image/jpeg" | "application/pdf";
            size_bytes: number;
            sha256: string;
            page_count: number;
            /** Format: date-time */
            created_at: string;
        };
        /** @description A completed event may include memory_context_count and memory_ids for explicit, relevant chat-context memories. Memory content is never included in event details. */
        WebConversationEvent: {
            sequence?: number;
            /** Format: uuid */
            message_id?: string;
            /** @enum {string} */
            event_type?: "accepted" | "queued" | "started" | "completed" | "failed" | "cancelled" | "cancellation_requested";
            /** Format: date-time */
            created_at?: string;
            details?: {
                memory_context_count?: number;
                memory_ids?: string[];
                attachment_context_count?: number;
                attachment_context_chars?: number;
                error_code?: string;
            } & {
                [key: string]: unknown;
            };
        };
        WebConversationRename: {
            /** @description Trimmed title must be non-blank and at most 160 UTF-8 bytes. */
            title: string;
        };
        WebAttachmentUploadResult: {
            attachment: components["schemas"]["WebAttachmentMetadata"];
            replayed: boolean;
        };
        WebMessageCreate: {
            content: string;
            attachment_ids?: string[];
        };
        WebMessageAccepted: {
            /** Format: uuid */
            conversation_id: string;
            /** Format: uuid */
            message_id: string;
            /** Format: uuid */
            response_id: string;
            /** @enum {string} */
            status: "queued" | "running" | "completed" | "failed" | "cancelled";
            replayed: boolean;
        };
        WebMessageCancelResult: {
            /** Format: uuid */
            conversation_id: string;
            /** Format: uuid */
            message_id: string;
            /** @enum {string} */
            status: "queued" | "running" | "completed" | "failed" | "cancelled" | "cancellation_requested";
        };
        /** @description Memories are stored owner-scoped and are not automatically injected into chat unless explicitly tagged `chat_context`. `sensitive`, `private`, `no_chat`, and `never_inject` tags override that opt-in. */
        WebMemoryCreate: {
            content: string;
            title?: string | null;
            tags?: string[];
        };
        WebMemoryCreateResult: {
            memory: components["schemas"]["WebMemory"];
            replayed: boolean;
        };
        WebMemory: {
            id: string;
            title: string;
            content: string;
            tags: string[];
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            updated_at: string;
        };
        WebMemoryList: {
            items: components["schemas"]["WebMemory"][];
            /** @constant */
            search_mode: "substring";
        };
        WebSupervisorReadiness: {
            /** @enum {string} */
            status: "ready" | "degraded";
            components: {
                authentication: {
                    /** @constant */
                    browser_session: "authenticated";
                    /** @enum {string} */
                    codex_sdk: "not_reported" | "missing" | "credential_present_unverified" | "verified_this_process" | "unavailable";
                };
                device_mapping: components["schemas"]["WebComponentReadiness"] & {
                    active_device_count: number;
                };
                engine: components["schemas"]["WebComponentReadiness"] & {
                    /** @enum {string} */
                    check?: "preflight" | "runtime";
                };
                supervisor_worker: components["schemas"]["WebComponentReadiness"];
                automation_scheduler: components["schemas"]["WebComponentReadiness"];
            };
        };
        WebComponentReadiness: {
            /** @enum {string} */
            state: "ready" | "unavailable" | "unknown";
            reason?: string;
        };
        WebTaskCreate: {
            objective: string;
            capability_scope: ("read_only" | "mac_read" | "assistant" | "development")[];
            idempotency_key: string;
            requester_device_id: string;
        };
        WebTaskResult: {
            task_id: string;
            /** @enum {string} */
            state: "CREATED" | "PLANNING" | "RUNNING" | "VERIFYING" | "WAITING_APPROVAL" | "PAUSED" | "COMPLETED" | "FAILED" | "CANCELLED";
            summary: string;
            result_text?: string | null;
            result_digest?: string | null;
            idempotent_replay: boolean;
            error_code?: string | null;
        };
        WebTaskEventPage: {
            task_id: string;
            events: components["schemas"]["WebTaskEvent"][];
            next_seq: number;
        };
        WebTaskEvent: {
            seq: number;
            event_type: string;
            /** Format: date-time */
            created_at: string;
            payload: {
                [key: string]: unknown;
            };
        };
        WebTaskCancellation: {
            task_id: string;
            /** @enum {string} */
            state: "CREATED" | "PLANNING" | "RUNNING" | "VERIFYING" | "WAITING_APPROVAL" | "PAUSED" | "COMPLETED" | "FAILED" | "CANCELLED";
            cancelled: boolean;
            action_may_have_started: boolean;
        };
        Approval: {
            id: string;
            task_id: string;
            operation: string;
            target_resource: string;
            canonical_arguments?: {
                [key: string]: unknown;
            };
            arguments?: {
                [key: string]: unknown;
            };
            operation_digest: string;
            reason?: string;
            /** @constant */
            risk?: "requires_user_approval";
            status: string;
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            expires_at: string;
            requester_device_id: string;
        };
        WebApprovalDecision: {
            /** @enum {string} */
            decision: "approve" | "reject";
            task_id?: string | null;
            expected_operation_digest: string;
            idempotency_key: string;
            requester_device_id: string;
            continuation_objective?: string | null;
        };
        Artifact: {
            artifact_id: string;
            task_id: string;
            file_name: string;
            size_bytes: number;
            archive_sha256: string;
            manifest_sha256: string;
            files: {
                [key: string]: unknown;
            }[];
            download_path: string;
        };
        WebDeviceList: {
            devices: components["schemas"]["WebDevice"][];
            count: number;
        };
        WebDevice: {
            device_id: string;
            device_name: string;
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            expires_at: string;
            /** @constant */
            status: "active";
        };
        WebPairingInvitationCreate: {
            /** @constant */
            transport: "wifi";
        };
        WebPairingInvitationResponse: {
            /** @constant */
            protocol: "openjarvis-pairing-v2";
            /** Format: uri */
            server_url: string;
            /**
             * Format: uri
             * @description Optional signed loopback API URL used only by an explicitly configured USB reverse tunnel.
             */
            loopback_url?: string;
            /** Format: ipv4 */
            gateway_address: string;
            /** @enum {string} */
            gateway_port: "8443" | "8444";
            gateway_certificate_sha256: string;
            server_fingerprint: string;
            server_public_key: string;
            invitation_id: string;
            challenge: string;
            /** Format: date-time */
            expires_at: string;
            server_signature: string;
            qr_payload: components["schemas"]["PairingQrPayloadV2"];
        };
        PairingQrPayloadV2: {
            /** @constant */
            protocol: "openjarvis-pairing-v2";
            /** Format: uri */
            server_url: string;
            /**
             * Format: uri
             * @description Optional signed loopback API URL used only by an explicitly configured USB reverse tunnel.
             */
            loopback_url?: string;
            /** Format: ipv4 */
            gateway_address: string;
            /** @enum {string} */
            gateway_port: "8443" | "8444";
            gateway_certificate_sha256: string;
            server_fingerprint: string;
            server_public_key: string;
            invitation_id: string;
            challenge: string;
            /** Format: date-time */
            expires_at: string;
            server_signature: string;
        };
        WebPairingRequestList: {
            requests: components["schemas"]["WebPairingRequest"][];
            count: number;
        };
        WebPairingRequest: {
            id: string;
            /** @constant */
            status: "PENDING_CONFIRMATION";
            device_name: string;
            device_fingerprint: string;
            /** Format: date-time */
            requested_at?: string | null;
            /** Format: date-time */
            expires_at: string;
        };
        WebPairingDecision: {
            /** @enum {string} */
            decision: "approve" | "reject";
        };
        WebPairingDecisionResult: {
            invitation_id: string;
            /** @enum {string} */
            status: "APPROVED" | "REJECTED" | "EXPIRED" | "CANCELLED";
        };
        WebPairingInvitationStatus: {
            invitation_id: string;
            /** @enum {string} */
            status: "INVITED" | "PENDING_CONFIRMATION" | "APPROVED" | "REJECTED" | "EXPIRED" | "CANCELLED" | "TOKEN_ISSUED";
        };
        WebPairingCancelRequest: Record<string, never>;
        WebDeviceRevokeResult: {
            /** @enum {string} */
            status: "revoked" | "already_revoked";
            device_id: string;
            /** @enum {string} */
            notification_token_cleanup: "none" | "revoked";
        };
        WebAutomation: {
            id: string;
            prompt: string;
            /** @enum {string} */
            schedule_type: "once" | "interval" | "cron";
            schedule_value: string;
            timezone?: string | null;
            /** @enum {string} */
            status: "active" | "paused" | "completed" | "cancelled";
            /** Format: date-time */
            next_run?: string | null;
            /** Format: date-time */
            last_run?: string | null;
            last_supervisor_task_id: string | null;
        };
        WebAutomationCreate: {
            prompt: string;
            /** @enum {string} */
            schedule_type: "once" | "interval" | "cron";
            schedule_value: string;
            timezone?: string | null;
            device_id: string;
        };
        WebAutomationCapabilities: {
            /** @constant */
            status: "available";
            operations: {
                [key: string]: boolean;
            };
            unavailable_reasons: {
                [key: string]: string;
            };
        };
        WebAutomationDetail: {
            automation: components["schemas"]["WebAutomation"];
            latest_supervisor_task: {
                [key: string]: unknown;
            } | null;
        };
        WebAutomationUpdate: {
            prompt?: string;
            /** @enum {string} */
            schedule_type?: "once" | "interval" | "cron";
            schedule_value?: string;
            timezone?: string;
        };
        WebAutomationRun: {
            /** Format: date-time */
            started_at: string;
            /** Format: date-time */
            finished_at: string | null;
            success: boolean;
            state: string;
            supervisor_task_id?: string | null;
            idempotency_key?: string | null;
            result: string;
            error: string;
            supervisor_task: {
                [key: string]: unknown;
            } | null;
        };
    };
    responses: {
        /** @description Missing, invalid, or expired credential */
        Unauthorized: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["Error"];
            };
        };
        /** @description Authenticated caller is not authorized for this resource */
        Forbidden: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["Error"];
            };
        };
        /** @description Backend, engine, or resource unavailable */
        Unavailable: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["Error"];
            };
        };
        /** @description Resource not found */
        NotFound: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["Error"];
            };
        };
        /** @description Request validation failure */
        InvalidRequest: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["Error"];
            };
        };
        /** @description State, ownership, digest, expiry, or idempotency conflict */
        Conflict: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["Error"];
            };
        };
    };
    parameters: {
        WebConversationId: string;
        TaskId: string;
        ApprovalId: string;
        PairingInvitationId: string;
        WebDeviceId: string;
    };
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    createWebSession: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WebLoginRequest"];
            };
        };
        responses: {
            /** @description Authenticated owner and CSRF value; session cookie is set. */
            200: {
                headers: {
                    "Set-Cookie"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebSession"];
                };
            };
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
            503: components["responses"]["Unavailable"];
        };
    };
    getWebSession: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Current browser-session metadata and CSRF value. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebSession"];
                };
            };
            401: components["responses"]["Unauthorized"];
            503: components["responses"]["Unavailable"];
        };
    };
    revokeWebSession: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Session revoked and cookies expired. */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
        };
    };
    listWebConversations: {
        parameters: {
            query?: {
                include_archived?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Owner-scoped conversations. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["WebConversation"][];
                    };
                };
            };
            401: components["responses"]["Unauthorized"];
        };
    };
    createWebConversation: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WebConversationCreate"];
            };
        };
        responses: {
            /** @description Conversation created. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebConversation"];
                };
            };
            401: components["responses"]["Unauthorized"];
        };
    };
    getWebConversation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Owner-scoped conversation with persisted messages and lifecycle events. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebConversationDetail"];
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    deleteWebConversation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Conversation and owned history deleted. */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    renameWebConversation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WebConversationRename"];
            };
        };
        responses: {
            /** @description Renamed owner-scoped conversation; title is persisted. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebConversation"];
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
            /** @description Title is blank after trimming or exceeds the supported UTF-8 length. */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    archiveWebConversation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Conversation archived. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    uploadWebConversationAttachment: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "X-File-Name"?: string;
            };
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "image/png": string;
                "image/jpeg": string;
                "application/pdf": string;
            };
        };
        responses: {
            /** @description Attachment stored and text extraction completed; OCR text is not returned. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebAttachmentUploadResult"];
                };
            };
            400: components["responses"]["InvalidRequest"];
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
            404: components["responses"]["NotFound"];
            409: components["responses"]["Conflict"];
            /** @description File or per-conversation storage limit exceeded. */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Unsupported media type or file signature. */
            415: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Malformed document or no extractable text. */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Document extractor concurrency limit reached; retry after a short delay. */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            503: components["responses"]["Unavailable"];
        };
    };
    downloadWebConversationAttachment: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
                attachment_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Original owner-uploaded attachment. */
            200: {
                headers: {
                    "X-Content-SHA256"?: string;
                    "Content-Disposition"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "image/png": string;
                    "image/jpeg": string;
                    "application/pdf": string;
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    submitWebConversationMessage: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WebMessageCreate"];
            };
        };
        responses: {
            /** @description User message persisted and response work accepted. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebMessageAccepted"];
                };
            };
            400: components["responses"]["InvalidRequest"];
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
            409: components["responses"]["Conflict"];
        };
    };
    cancelWebConversationMessage: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
                message_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Current cancellation state. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebMessageCancelResult"];
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    streamWebConversationMessage: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                conversation_id: components["parameters"]["WebConversationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WebMessageCreate"];
            };
        };
        responses: {
            /** @description Authenticated server-sent events for acceptance, lifecycle, deltas, and completion. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "text/event-stream": string;
                };
            };
            400: components["responses"]["InvalidRequest"];
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
            409: components["responses"]["Conflict"];
            503: components["responses"]["Unavailable"];
        };
    };
    listWebMemory: {
        parameters: {
            query?: {
                limit?: number;
                offset?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Owner-scoped memories, ordered by most recently updated. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebMemoryList"];
                };
            };
            401: components["responses"]["Unauthorized"];
        };
    };
    createWebMemory: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WebMemoryCreate"];
            };
        };
        responses: {
            /** @description Memory persisted or returned as an idempotent replay. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebMemoryCreateResult"];
                };
            };
            400: components["responses"]["InvalidRequest"];
            401: components["responses"]["Unauthorized"];
            409: components["responses"]["Conflict"];
            /** @description Owner-scoped memory quota exceeded. */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            422: components["responses"]["InvalidRequest"];
        };
    };
    searchWebMemory: {
        parameters: {
            query: {
                q: string;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Owner-scoped substring matches. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebMemoryList"];
                };
            };
            401: components["responses"]["Unauthorized"];
            422: components["responses"]["InvalidRequest"];
        };
    };
    getWebMemory: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                memory_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Owned memory item. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebMemory"];
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    deleteWebMemory: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                memory_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Owned memory deleted. */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    getWebSupervisorReadiness: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Supervisor, engine, device-mapping, and automation readiness. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebSupervisorReadiness"];
                };
            };
            401: components["responses"]["Unauthorized"];
        };
    };
    listWebSupervisorTasks: {
        parameters: {
            query?: {
                state?: "active" | "completed" | "all";
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Tasks owned by explicitly mapped active paired devices. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        tasks: components["schemas"]["WebTaskResult"][];
                    };
                };
            };
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
        };
    };
    submitWebSupervisorTask: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WebTaskCreate"];
            };
        };
        responses: {
            /** @description Completed idempotent replay; no worker wake occurs. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebTaskResult"];
                };
            };
            /** @description Newly accepted task or nonterminal idempotent replay. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebTaskResult"];
                };
            };
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
            409: components["responses"]["Conflict"];
            503: components["responses"]["Unavailable"];
        };
    };
    getWebSupervisorTask: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_id: components["parameters"]["TaskId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Owner-scoped durable task state and result. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebTaskResult"];
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    getWebSupervisorTaskEvents: {
        parameters: {
            query?: {
                after_seq?: number;
                limit?: number;
            };
            header?: never;
            path: {
                task_id: components["parameters"]["TaskId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Sanitized durable event page. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebTaskEventPage"];
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    streamWebSupervisorTaskEvents: {
        parameters: {
            query?: {
                after_seq?: number;
            };
            header?: {
                "Last-Event-ID"?: string;
            };
            path: {
                task_id: components["parameters"]["TaskId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Sanitized task lifecycle event stream with reconnect replay. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "text/event-stream": string;
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
            503: components["responses"]["Unavailable"];
        };
    };
    cancelWebSupervisorTask: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_id: components["parameters"]["TaskId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Current cancellation state; an operation already started may not be reversible. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebTaskCancellation"];
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
            409: components["responses"]["Conflict"];
        };
    };
    listWebPendingApprovals: {
        parameters: {
            query?: {
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Pending exact-operation approvals for mapped devices. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        approvals: components["schemas"]["Approval"][];
                        count: number;
                    };
                };
            };
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
        };
    };
    getWebApproval: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                approval_id: components["parameters"]["ApprovalId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Exact approval details for the mapped task owner. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Approval"];
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    decideWebApproval: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                approval_id: components["parameters"]["ApprovalId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WebApprovalDecision"];
            };
        };
        responses: {
            /** @description Decision and actual execution/continuation result. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
            404: components["responses"]["NotFound"];
            409: components["responses"]["Conflict"];
        };
    };
    listWebTaskArtifacts: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_id: components["parameters"]["TaskId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Artifacts for a completed development task owned by a mapped device. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        task_id: string;
                        artifacts: components["schemas"]["Artifact"][];
                    };
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
            409: components["responses"]["Conflict"];
        };
    };
    downloadWebTaskArtifact: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_id: components["parameters"]["TaskId"];
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description ZIP file with SHA-256 integrity header. */
            200: {
                headers: {
                    "X-Content-SHA256"?: string;
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/zip": string;
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
            409: components["responses"]["Conflict"];
        };
    };
    listWebDevices: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Safe metadata for explicitly mapped active paired devices. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebDeviceList"];
                };
            };
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
        };
    };
    createWebPairingInvitation: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WebPairingInvitationCreate"];
            };
        };
        responses: {
            /** @description Signed v2 QR payload. Response is no-store. */
            200: {
                headers: {
                    "Cache-Control"?: "no-store";
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebPairingInvitationResponse"];
                };
            };
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
            /** @description Local Wi-Fi pairing is disabled or its configured identity/listener is unavailable. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            422: components["responses"]["InvalidRequest"];
            503: components["responses"]["Unavailable"];
        };
    };
    listWebPairingRequests: {
        parameters: {
            query?: {
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Unexpired devices that proved possession and await owner confirmation. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebPairingRequestList"];
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
            503: components["responses"]["Unavailable"];
        };
    };
    decideWebPairingRequest: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                invitation_id: components["parameters"]["PairingInvitationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WebPairingDecision"];
            };
        };
        responses: {
            /** @description Owner decision recorded or invitation expired before decision. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebPairingDecisionResult"];
                };
            };
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
            404: components["responses"]["NotFound"];
            409: components["responses"]["Conflict"];
            422: components["responses"]["InvalidRequest"];
        };
    };
    getWebPairingInvitationStatus: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                invitation_id: components["parameters"]["PairingInvitationId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Pairing status for the invitation. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebPairingInvitationStatus"];
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    cancelWebPairingInvitation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                invitation_id: components["parameters"]["PairingInvitationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WebPairingCancelRequest"];
            };
        };
        responses: {
            /** @description Invitation cancelled, expired, or already rejected. Issued device credentials are not revoked. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebPairingDecisionResult"];
                };
            };
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
            404: components["responses"]["NotFound"];
            409: components["responses"]["Conflict"];
            422: components["responses"]["InvalidRequest"];
        };
    };
    getWebDevice: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                device_id: components["parameters"]["WebDeviceId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Safe active metadata; no key material or token hash. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebDevice"];
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    revokeWebDevice: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                device_id: components["parameters"]["WebDeviceId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    /** @constant */
                    confirm: true;
                };
            };
        };
        responses: {
            /** @description Revocation and FCM cleanup state. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebDeviceRevokeResult"];
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
            409: components["responses"]["Conflict"];
            503: components["responses"]["Unavailable"];
        };
    };
    listWebAutomations: {
        parameters: {
            query?: {
                status?: "active" | "paused" | "completed" | "cancelled";
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Owner-scoped Web automation records. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["WebAutomation"][];
                    };
                };
            };
            401: components["responses"]["Unauthorized"];
            503: components["responses"]["Unavailable"];
        };
    };
    createWebAutomation: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WebAutomationCreate"];
            };
        };
        responses: {
            /** @description Persisted automation record. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        /** @constant */
                        status: "created";
                        automation: components["schemas"]["WebAutomation"];
                    };
                };
            };
            400: components["responses"]["InvalidRequest"];
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
            409: components["responses"]["Conflict"];
            422: components["responses"]["InvalidRequest"];
            503: components["responses"]["Unavailable"];
        };
    };
    getWebAutomationCapabilities: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Capability availability based on active scheduler, mapping, and Link runner. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebAutomationCapabilities"];
                };
            };
            401: components["responses"]["Unauthorized"];
            503: components["responses"]["Unavailable"];
        };
    };
    getWebAutomation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                automation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Owner-scoped automation with latest correlated supervisor state. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebAutomationDetail"];
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
            503: components["responses"]["Unavailable"];
        };
    };
    deleteWebAutomation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                automation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Automation cancelled; history retained. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    updateWebAutomation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                automation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WebAutomationUpdate"];
            };
        };
        responses: {
            /** @description Updated owner-scoped automation. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WebAutomation"];
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
            409: components["responses"]["Conflict"];
            422: components["responses"]["InvalidRequest"];
        };
    };
    getWebAutomationHistory: {
        parameters: {
            query?: {
                limit?: number;
            };
            header?: never;
            path: {
                automation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Persisted occurrence history with current correlated Link task status. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["WebAutomationRun"][];
                    };
                };
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
            503: components["responses"]["Unavailable"];
        };
    };
    enableWebAutomation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                automation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Automation resumed. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    disableWebAutomation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                automation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Automation paused. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    pauseWebAutomation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                automation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Automation paused. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
        };
    };
    triggerWebAutomation: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                automation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Manual occurrence dispatch state and supervisor task correlation. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            400: components["responses"]["InvalidRequest"];
            401: components["responses"]["Unauthorized"];
            404: components["responses"]["NotFound"];
            503: components["responses"]["Unavailable"];
        };
    };
}
