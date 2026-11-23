// Generated from openapi/thozha-v1.yaml at e7ae0cd225edc64ec4178bf765ebbb01399f75a8.
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
        patch?: never;
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
        };
        WebConversationEvent: {
            sequence?: number;
            /** Format: uuid */
            message_id?: string;
            /** @enum {string} */
            event_type?: "accepted" | "queued" | "started" | "completed" | "failed" | "cancelled" | "cancellation_requested";
            /** Format: date-time */
            created_at?: string;
            details?: {
                [key: string]: unknown;
            };
        };
        WebMessageCreate: {
            content: string;
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
    };
    responses: {
        /** @description Missing, invalid, or expired credential */
        Unauthorized: {
            headers: {
                [name: string]: unknown;
            };
            content?: never;
        };
        /** @description Authenticated caller is not authorized for this resource */
        Forbidden: {
            headers: {
                [name: string]: unknown;
            };
            content?: never;
        };
        /** @description Backend, engine, or resource unavailable */
        Unavailable: {
            headers: {
                [name: string]: unknown;
            };
            content?: never;
        };
        /** @description Resource not found */
        NotFound: {
            headers: {
                [name: string]: unknown;
            };
            content?: never;
        };
        /** @description Request validation failure */
        InvalidRequest: {
            headers: {
                [name: string]: unknown;
            };
            content?: never;
        };
        /** @description State, ownership, digest, expiry, or idempotency conflict */
        Conflict: {
            headers: {
                [name: string]: unknown;
            };
            content?: never;
        };
    };
    parameters: {
        WebConversationId: string;
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
}
