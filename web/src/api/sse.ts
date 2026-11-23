import { z } from 'zod';
import type { WebConversationSseEvent } from '../../../contracts/web-api/v1';

const response = { response_id: z.uuid() };
const mode = z.enum(['engine_stream', 'buffered']);
const status = z.enum(['accepted', 'queued', 'running', 'completed', 'failed', 'cancelled']);
const events = z.discriminatedUnion('event', [
  z.object({
    event: z.literal('accepted'),
    data: z.object({
      ...response,
      conversation_id: z.uuid(),
      message_id: z.uuid(),
      status,
      replayed: z.boolean(),
    }),
  }),
  z.object({ event: z.literal('started'), data: z.object(response) }),
  z.object({ event: z.literal('stream_mode'), data: z.object({ ...response, mode }) }),
  z.object({ event: z.literal('delta'), data: z.object({ ...response, content: z.string() }) }),
  z.object({ event: z.literal('message'), data: z.object({ ...response, content: z.string() }) }),
  z.object({
    event: z.literal('done'),
    data: z.object({
      ...response,
      status,
      replayed: z.boolean().optional(),
      stream_mode: mode.optional(),
    }),
  }),
  z.object({
    event: z.literal('error'),
    data: z.object({ ...response, code: z.literal('generation_failed') }),
  }),
]) satisfies z.ZodType<WebConversationSseEvent>;
const maxEventSize = 1024 * 1024;

export async function readConversationStream(
  body: ReadableStream<Uint8Array>,
  conversationId: string,
  onEvent: (event: WebConversationSseEvent) => void
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let acceptedId: string | undefined;
  let finished = false;
  try {
    while (!finished) {
      const { value, done } = await reader.read();
      if (done) throw new Error('The response stream ended before a terminal event.');
      buffer += decoder.decode(value, { stream: true });
      let boundary: RegExpExecArray | null;
      while (!finished && (boundary = /\r?\n\r?\n/.exec(buffer))) {
        if (boundary.index > maxEventSize)
          throw new Error('Response stream event exceeds the size limit.');
        const block = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        let eventName = '';
        const lines: string[] = [];
        for (const line of block.split(/\r?\n/)) {
          if (line.startsWith('event:')) eventName = line.slice(6).trim();
          if (line.startsWith('data:')) lines.push(line.slice(5).replace(/^ /, ''));
        }
        if (!lines.length) continue;
        const data: unknown = JSON.parse(lines.join('\n'));
        const event = events.parse({ event: eventName, data });
        if (event.event === 'accepted') {
          if (acceptedId || event.data.conversation_id !== conversationId)
            throw new Error('Unexpected stream acceptance.');
          acceptedId = event.data.response_id;
        }
        if (!acceptedId || event.data.response_id !== acceptedId)
          throw new Error('Unexpected stream response ID.');
        onEvent(event);
        finished = event.event === 'done' || event.event === 'error';
      }
      if (!finished && buffer.length > maxEventSize)
        throw new Error('Response stream event exceeds the size limit.');
    }
  } finally {
    // Close a malformed or interrupted stream instead of leaving a host worker attached.
    void reader.cancel().catch(() => {
      /* The original read error is reported by the caller. */
    });
    reader.releaseLock();
  }
}
