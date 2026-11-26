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

describe('device pairing and conversation attachments', () => {
  const invitationId = 'a'.repeat(32);
  const qrPayload = {
    protocol: 'openjarvis-pairing-v2',
    server_url: 'https://192.0.2.10:8443',
    gateway_address: '192.0.2.10',
    gateway_port: '8443',
    gateway_certificate_sha256: `sha256:${'1'.repeat(64)}`,
    server_fingerprint: `sha256:${'2'.repeat(64)}`,
    server_public_key: 'fixture-public-key',
    invitation_id: invitationId,
    challenge: 'fixture-challenge',
    expires_at: '2026-11-23T00:05:00Z',
    server_signature: 'fixture-signature',
  };

  it('uses the owner session for invitation, status, pending-request, decision, and cancellation routes', async () => {
    const requestId = 'b'.repeat(32);
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json({ ...qrPayload, qr_payload: qrPayload }))
      .mockResolvedValueOnce(
        Response.json({ invitation_id: invitationId, status: 'PENDING_CONFIRMATION' })
      )
      .mockResolvedValueOnce(
        Response.json({
          count: 1,
          requests: [
            {
              id: requestId,
              status: 'PENDING_CONFIRMATION',
              device_name: 'Fixture phone',
              device_fingerprint: 'sha256:fixture',
              requested_at: '2026-11-23T00:00:01Z',
              expires_at: '2026-11-23T00:05:00Z',
            },
          ],
        })
      )
      .mockResolvedValueOnce(Response.json({ invitation_id: requestId, status: 'APPROVED' }))
      .mockResolvedValueOnce(Response.json({ invitation_id: invitationId, status: 'CANCELLED' }));
    const api = createWebApi(fetcher).authorized('test-csrf', vi.fn());

    await expect(api.createPairingInvitation()).resolves.toMatchObject({
      qr_payload: qrPayload,
    });
    await expect(api.getPairingInvitationStatus(invitationId)).resolves.toEqual({
      invitation_id: invitationId,
      status: 'PENDING_CONFIRMATION',
    });
    await expect(api.listPairingRequests()).resolves.toMatchObject({ count: 1 });
    await expect(api.decidePairingRequest(requestId, 'approve')).resolves.toEqual({
      invitation_id: requestId,
      status: 'APPROVED',
    });
    await expect(api.cancelPairingInvitation(invitationId)).resolves.toEqual({
      invitation_id: invitationId,
      status: 'CANCELLED',
    });

    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      '/v1/web/devices/pairing-invitations',
      `/v1/web/devices/pairing-invitations/${invitationId}`,
      '/v1/web/devices/pairing-requests?limit=50',
      `/v1/web/devices/pairing-requests/${requestId}/decision`,
      `/v1/web/devices/pairing-invitations/${invitationId}/cancel`,
    ]);
    const [createInit, , , decisionInit, cancelInit] = fetcher.mock.calls.map(([, init]) => init);
    expect(new Headers(createInit?.headers).get('X-Thozha-CSRF')).toBe('test-csrf');
    expect(JSON.parse(String(createInit?.body))).toEqual({ transport: 'wifi' });
    expect(JSON.parse(String(decisionInit?.body))).toEqual({ decision: 'approve' });
    expect(JSON.parse(String(cancelInit?.body))).toEqual({});
    expect(fetcher.mock.calls.every(([, init]) => init?.credentials === 'same-origin')).toBe(true);
  });

  it('accepts a signed portable gateway QR on the compatible alternate port', async () => {
    const portablePayload = {
      ...qrPayload,
      server_url: 'https://thozha-portable.local:8444',
      loopback_url: 'http://127.0.0.1:18000',
      gateway_port: '8444',
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(
      Response.json({ ...portablePayload, qr_payload: portablePayload })
    );
    const api = createWebApi(fetcher).authorized('test-csrf', vi.fn());
    await expect(api.createPairingInvitation()).resolves.toMatchObject({
      gateway_port: '8444',
      loopback_url: 'http://127.0.0.1:18000',
      qr_payload: portablePayload,
    });
  });

  it('uploads raw bytes with an idempotency key and safe metadata headers', async () => {
    const bytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47]);
    const file = new File([bytes], 'report-测试.png', { type: 'image/png' });
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (url, init) => {
      expect(url).toBe(`/v1/web/conversations/${conversation}/attachments`);
      expect(init?.method).toBe('POST');
      expect(init?.body).toBe(file);
      expect(await new Response(init?.body).arrayBuffer()).toEqual(bytes.buffer);
      const headers = new Headers(init?.headers);
      expect(headers.get('Content-Type')).toBe('image/png');
      expect(headers.get('X-File-Name')).toBe('report-__.png');
      expect(headers.get('Idempotency-Key')).toBe('stable-upload-key');
      expect(headers.get('X-Thozha-CSRF')).toBe('test-csrf');
      return Response.json(
        {
          attachment: {
            id: 'c'.repeat(32),
            file_name: 'report-__.png',
            media_type: 'image/png',
            size_bytes: bytes.byteLength,
            sha256: 'd'.repeat(64),
            page_count: 1,
            created_at: '2026-11-23T00:00:00Z',
          },
          replayed: false,
        },
        { status: 201 }
      );
    });
    const api = createWebApi(fetcher).authorized('test-csrf', vi.fn());
    await expect(
      api.uploadAttachment(conversation, file, 'image/png', 'stable-upload-key')
    ).resolves.toMatchObject({
      attachment: { file_name: 'report-__.png', size_bytes: 4 },
    });
  });

  it('reports measured transfer bytes without treating transfer completion as a saved attachment', async () => {
    const file = new File([new Uint8Array([1, 2, 3, 4])], 'report.png', {
      type: 'image/png',
    });
    const headers = new Map<string, string>();
    class ProgressUpload {
      status = 201;
      responseURL = `http://localhost:3000/v1/web/conversations/${conversation}/attachments`;
      responseText = JSON.stringify({
        attachment: {
          id: 'c'.repeat(32),
          file_name: 'report.png',
          media_type: 'image/png',
          size_bytes: 4,
          sha256: 'd'.repeat(64),
          page_count: 1,
          created_at: '2026-11-23T00:00:00Z',
        },
        replayed: false,
      });
      upload = { onprogress: null as ((event: ProgressEvent) => void) | null };
      onload: (() => void) | null = null;
      open(method: string, url: string) {
        expect(method).toBe('POST');
        expect(url).toBe(`/v1/web/conversations/${conversation}/attachments`);
      }
      setRequestHeader(name: string, value: string) {
        headers.set(name, value);
      }
      getResponseHeader() {
        return 'application/json';
      }
      send(body: File) {
        expect(body).toBe(file);
        this.upload.onprogress?.({ lengthComputable: true, loaded: 2, total: 4 } as ProgressEvent);
        this.upload.onprogress?.({ lengthComputable: true, loaded: 4, total: 4 } as ProgressEvent);
        this.onload?.();
      }
      abort() {
        throw new Error('An unrequested abort must not occur.');
      }
    }
    vi.stubGlobal('XMLHttpRequest', ProgressUpload);
    try {
      const progress: number[] = [];
      const api = createWebApi(vi.fn<typeof fetch>()).authorized('test-csrf', vi.fn());
      const uploaded = await api.uploadAttachment(
        conversation,
        file,
        'image/png',
        'stable-upload-key',
        undefined,
        (sent, total) => progress.push(Math.round((sent / total) * 100))
      );
      expect(progress).toEqual([50, 100]);
      expect(uploaded.attachment.file_name).toBe('report.png');
      expect(headers.get('X-Thozha-CSRF')).toBe('test-csrf');
      expect(headers.get('Idempotency-Key')).toBe('stable-upload-key');
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
