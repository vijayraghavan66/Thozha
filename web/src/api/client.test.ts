import { describe, expect, it, vi } from 'vitest';
import { createWebApi, WebApiError } from './client';

const conversation = '3a8199b0-0f23-4ba1-b16a-ae4b381c7a49';
const message = '9fa99b61-7884-40a9-b3ad-481b53d37728';
const result = '046e83cf-6827-4e29-97b2-6674cfd15bb0';

describe('browser Web API', () => {
  it('persists conversation titles through the owner-scoped PATCH contract', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      expect(input).toBe(`/v1/web/conversations/${conversation}`);
      expect(init).toMatchObject({
        credentials: 'same-origin',
        mode: 'same-origin',
        redirect: 'error',
        cache: 'no-store',
        method: 'PATCH',
      });
      expect(JSON.parse(String(init?.body))).toEqual({ title: 'Renamed conversation' });
      expect(new Headers(init?.headers).get('X-Thozha-CSRF')).toBe('test-csrf');
      return Response.json({
        id: conversation,
        title: 'Renamed conversation',
        created_at: '2026-11-23T00:00:00Z',
        updated_at: '2026-11-23T00:01:00Z',
        archived_at: null,
      });
    });
    const api = createWebApi(fetcher).authorized('test-csrf', vi.fn());
    await expect(api.rename(conversation, 'Renamed conversation')).resolves.toMatchObject({
      id: conversation,
      title: 'Renamed conversation',
    });
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it('uses only same-origin cookie authentication, CSRF, and the caller’s replay key', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      expect(input).toBe(`/v1/web/conversations/${conversation}/messages`);
      expect(init).toMatchObject({
        credentials: 'same-origin',
        mode: 'same-origin',
        redirect: 'error',
        cache: 'no-store',
        method: 'POST',
      });
      const headers = new Headers(init?.headers);
      expect(headers.has('Authorization')).toBe(false);
      expect(headers.get('X-Thozha-CSRF')).toBe('test-csrf');
      expect(headers.get('Idempotency-Key')).toBe('test-identical-submission-key');
      expect(JSON.parse(String(init?.body))).toEqual({ content: 'Check persistence' });
      return Response.json(
        {
          conversation_id: conversation,
          message_id: message,
          response_id: result,
          status: 'queued',
          replayed: false,
        },
        { status: 202 }
      );
    });
    const api = createWebApi(fetcher).authorized('test-csrf', vi.fn());
    await expect(
      api.submit(conversation, { content: 'Check persistence' }, 'test-identical-submission-key')
    ).resolves.toMatchObject({ response_id: result, status: 'queued' });
  });

  it('invalidates an expired session instead of treating it as empty data', async () => {
    const expired = vi.fn();
    const api = createWebApi(
      vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 401 }))
    ).authorized('test-csrf', expired);
    await expect(api.list()).rejects.toMatchObject({ status: 401 });
    expect(expired).toHaveBeenCalledOnce();
  });

  it('does not accept SPA fallback HTML or incomplete JSON as a restored session', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response('<html>Workspace</html>', { headers: { 'Content-Type': 'text/html' } })
      )
      .mockResolvedValueOnce(Response.json({ user_id: 'example' }));
    const api = createWebApi(fetcher);
    await expect(api.session()).rejects.toEqual(new WebApiError('html'));
    await expect(api.session()).rejects.toEqual(new WebApiError('protocol'));
  });
});

describe('owner-scoped supervisor API', () => {
  const taskId = '1'.repeat(32);
  const deviceId = '2'.repeat(32);

  it('accepts a completed idempotent replay without changing the original request', async () => {
    const body = {
      requester_device_id: deviceId,
      objective: 'Read status only',
      capability_scope: ['read_only'] as const,
      idempotency_key: 'stable-original-task-key',
    };
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (url, init) => {
      expect(url).toBe('/v1/web/tasks');
      expect(init?.method).toBe('POST');
      expect(JSON.parse(String(init?.body))).toEqual(body);
      expect(new Headers(init?.headers).get('X-Thozha-CSRF')).toBe('test-csrf');
      return Response.json(
        {
          task_id: taskId,
          state: 'COMPLETED',
          summary: 'saved',
          idempotent_replay: true,
        },
        { status: 200 }
      );
    });
    const api = createWebApi(fetcher).authorized('test-csrf', vi.fn());
    await expect(
      api.submitTask({ ...body, capability_scope: [...body.capability_scope] })
    ).resolves.toMatchObject({
      task_id: taskId,
      state: 'COMPLETED',
      idempotent_replay: true,
    });
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it('forwards only safe server errors for control routes, never validation input or login details', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        Response.json({ detail: 'Supervisor task worker is unavailable' }, { status: 503 })
      )
      .mockResolvedValueOnce(
        Response.json({ detail: [{ input: 'private prompt' }] }, { status: 422 })
      )
      .mockResolvedValueOnce(
        Response.json({ detail: 'private authentication error' }, { status: 401 })
      );
    const api = createWebApi(fetcher);
    const control = api.authorized('test-csrf', vi.fn());
    const body = {
      requester_device_id: deviceId,
      objective: 'Test',
      capability_scope: ['read_only' as const],
      idempotency_key: 'test-key-repeat',
    };
    await expect(control.submitTask(body)).rejects.toMatchObject({
      status: 503,
      detail: 'Supervisor task worker is unavailable',
    });
    await expect(control.submitTask(body)).rejects.toMatchObject({
      status: 422,
      detail: 'The host rejected one or more request fields.',
    });
    await expect(api.login({ username: 'test', password: 'temporary' })).rejects.toMatchObject({
      status: 401,
      detail: undefined,
    });
  });

  it('rejects a mismatched task event frame and resumes only past the saved sequence', async () => {
    const event = {
      seq: 9,
      event_type: 'task.state',
      created_at: '2026-11-23T00:00:00Z',
      payload: { to: 'RUNNING' },
    };
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (path, init) => {
      expect(path).toBe(`/v1/web/tasks/${taskId}/events/stream?after_seq=7`);
      expect(new Headers(init?.headers).get('Last-Event-ID')).toBe('7');
      return new Response(
        `: keep-alive\n\nid: 9\nevent: task.state\ndata: ${JSON.stringify(event)}\n\n` +
          `id: 10\nevent: task.state\ndata: ${JSON.stringify(event)}\n\n`,
        { headers: { 'Content-Type': 'text/event-stream' } }
      );
    });
    const onEvent = vi.fn();
    const api = createWebApi(fetcher).authorized('test-csrf', vi.fn());
    await expect(
      api.streamTaskEvents(taskId, 7, new AbortController().signal, vi.fn(), onEvent)
    ).rejects.toMatchObject({ kind: 'protocol' });
    expect(onEvent.mock.calls).toEqual([[event]]);
  });
});
