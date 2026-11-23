import { z } from 'zod';
import type { WebConversationSseEvent } from '../../../contracts/web-api/v1';
import { WebApiError } from './errors';

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

type Frame = { event: string; id: string; data: unknown };

export async function readEventStream(
  body: ReadableStream<Uint8Array>,
  onFrame: (frame: Frame) => boolean | void,
  idleTimeout?: number
): Promise<boolean> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  async function read() {
    if (!idleTimeout) return reader.read();
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([
        reader.read(),
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new WebApiError('timeout')), idleTimeout);
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  }
  try {
    for (;;) {
      const { value, done } = await read();
      if (done) {
        if (buffer.trim()) throw new Error('Incomplete SSE frame.');
        return false;
      }
      buffer += decoder.decode(value, { stream: true });
      let boundary: RegExpExecArray | null;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        if (boundary.index > maxEventSize)
          throw new Error('Response stream event exceeds the size limit.');
        const block = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        let event = '';
        let id = '';
        const lines: string[] = [];
        for (const line of block.split(/\r?\n/)) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          if (line.startsWith('id:')) id = line.slice(3).trim();
          if (line.startsWith('data:')) lines.push(line.slice(5).replace(/^ /, ''));
        }
        if (!lines.length) continue;
        const data: unknown = JSON.parse(lines.join('\n'));
        if (onFrame({ event, id, data })) return true;
      }
      if (buffer.length > maxEventSize)
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

export async function readConversationStream(
  body: ReadableStream<Uint8Array>,
  conversationId: string,
  onEvent: (event: WebConversationSseEvent) => void
): Promise<void> {
  let acceptedId: string | undefined;
  const finished = await readEventStream(body, ({ event: name, data }) => {
    const event = events.parse({ event: name, data });
    if (event.event === 'accepted') {
      if (acceptedId || event.data.conversation_id !== conversationId)
        throw new Error('Unexpected stream acceptance.');
      acceptedId = event.data.response_id;
    }
    if (!acceptedId || event.data.response_id !== acceptedId)
      throw new Error('Unexpected stream response ID.');
    onEvent(event);
    return event.event === 'done' || event.event === 'error';
  });
  if (!finished) throw new Error('The response stream ended before a terminal event.');
}
