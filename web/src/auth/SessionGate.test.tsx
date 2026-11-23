import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { createWebApi } from '../api/client';
import { SessionGate } from './SessionGate';

it('fails closed when Web authentication is disabled, then retries the real session check', async () => {
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(new Response(null, { status: 503 }))
    .mockResolvedValueOnce(new Response(null, { status: 401 }));
  render(<SessionGate backend={createWebApi(fetcher)} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Confirm browser authentication');
  expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
  expect(await screen.findByLabelText('Password')).toBeInTheDocument();
  expect(fetcher.mock.calls.map(([path]) => path)).toEqual(['/v1/web/session', '/v1/web/session']);
});

it('does not reveal workspace data if a login response arrived but the browser rejected the cookie', async () => {
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(new Response(null, { status: 401 }))
    .mockResolvedValueOnce(
      Response.json({ user_id: 'fixture', csrf_token: 'test-csrf', expires_at: 9_999_999_999 })
    )
    .mockResolvedValueOnce(new Response(null, { status: 401 }));
  render(<SessionGate backend={createWebApi(fetcher)} />);
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText('Workspace ID'), 'fixture');
  await user.type(screen.getByLabelText('Password'), 'test-only-password');
  await user.click(screen.getByRole('button', { name: 'Sign in' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('did not keep the session cookie');
  expect(screen.getByLabelText('Password')).toHaveValue('');
  expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
});
