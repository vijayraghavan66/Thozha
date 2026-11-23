import { describe, expect, it, vi } from 'vitest';
import { createWebApi, WebApiError } from './client';

const conversation = '3a8199b0-0f23-4ba1-b16a-ae4b381c7a49';
const message = '9fa99b61-7884-40a9-b3ad-481b53d37728';
const result = '046e83cf-6827-4e29-97b2-6674cfd15bb0';

describe('browser Web API', () => {
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
    await expect(api.session()).rejects.toEqual(new WebApiError('protocol'));
    await expect(api.session()).rejects.toEqual(new WebApiError('protocol'));
  });
});
