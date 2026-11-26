import { expect, it, vi } from 'vitest';
import { readConversationStream } from './sse';

const conversation_id = '3a8199b0-0f23-4ba1-b16a-ae4b381c7a49';
const message_id = '9fa99b61-7884-40a9-b3ad-481b53d37728';
const response_id = '046e83cf-6827-4e29-97b2-6674cfd15bb0';
const frame = (name: string, data: object) =>
  `event: ${name}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`;
const accepted = frame('accepted', {
  conversation_id,
  message_id,
  response_id,
  status: 'running',
  replayed: false,
});
function wire(text: string) {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const byte of new TextEncoder().encode(text)) controller.enqueue(Uint8Array.of(byte));
      controller.close();
    },
  });
}

it('decodes real frames across chunk, CRLF, and UTF-8 boundaries', async () => {
  const publish = vi.fn();
  await readConversationStream(
    wire(
      accepted +
        frame('stream_mode', { response_id, mode: 'engine_stream' }) +
        frame('delta', { response_id, content: 'Café' }) +
        frame('done', { response_id, status: 'completed' })
    ),
    conversation_id,
    publish
  );
  expect(publish.mock.calls.map(([event]) => event.event)).toEqual([
    'accepted',
    'stream_mode',
    'delta',
    'done',
  ]);
  expect(publish.mock.calls[2][0].data.content).toBe('Café');
});

it('refuses to render data for a different response and rejects incomplete streams', async () => {
  const publish = vi.fn();
  await expect(
    readConversationStream(
      wire(accepted + frame('delta', { response_id: message_id, content: 'wrong conversation' })),
      conversation_id,
      publish
    )
  ).rejects.toThrow('Unexpected stream response ID');
  expect(publish).toHaveBeenCalledOnce();
  await expect(readConversationStream(wire(accepted), conversation_id, vi.fn())).rejects.toThrow(
    'before a terminal event'
  );
});
