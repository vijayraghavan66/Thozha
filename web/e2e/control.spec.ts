import { createHash, randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { signIn } from './session';

const studio = '/#/developer-studio';
const taskPath = '/v1/web/tasks';
const readinessPath = '/v1/web/supervisor/readiness';
const authHeaders = async (page: Page) => {
  const response = await page.request.get('/v1/web/session');
  expect(response.status()).toBe(200);
  const session = await response.json();
  return { Origin: new URL(page.url()).origin, 'X-Thozha-CSRF': session.csrf_token as string };
};
const pairedDevice = async (page: Page) => {
  const picker = page.getByRole('combobox', { name: 'Requester device', exact: true });
  const value = await picker
    .locator('option')
    .filter({ hasText: 'Isolated test pairing' })
    .getAttribute('value');
  expect(value).toBeTruthy();
  await picker.selectOption(value!);
};

async function submitTask(page: Page, objective?: string, scope = 'read_only') {
  if (objective) {
    await page.getByRole('button', { name: 'New supervisor task' }).click();
    await page.getByLabel('Objective').fill(objective);
  }
  await pairedDevice(page);
  await page.getByLabel('Capability scope').selectOption(scope);
  const accepted = page.waitForResponse(
    response => response.url().endsWith(taskPath) && response.request().method() === 'POST'
  );
  await page.getByRole('button', { name: 'Submit task', exact: true }).click();
  const response = await accepted;
  expect(response.status()).toBe(202);
  expect(response.request().headers()).not.toHaveProperty('authorization');
  const body = await response.json();
  await expect(page).toHaveURL(new RegExp(`task=${body.task_id}$`));
  return { id: body.task_id as string, request: response.request().postDataJSON() };
}

test('prefills without execution, resumes real supervisor SSE, and reloads the verified result', async ({
  page,
}) => {
  await signIn(page, '/#/ai-workspace');
  await page.locator('main').getByRole('button', { name: '＋ New conversation' }).click();
  const composer = page.getByRole('textbox', { name: 'Message Jarvis' });
  await composer.fill('/task Fixture slow task');
  const writes: string[] = [];
  page.on('request', request => {
    if (request.method() === 'POST') writes.push(new URL(request.url()).pathname);
  });
  await page.getByRole('button', { name: 'Review task', exact: true }).click();
  await expect(page.getByLabel('Objective')).toHaveValue('Fixture slow task');
  expect(writes).toEqual([]);

  const requests: { cursor: string | null; header?: string }[] = [];
  await page.route(/\/v1\/web\/tasks\/[a-f0-9]{32}\/events\/stream\?/, async route => {
    const request = route.request();
    requests.push({
      cursor: new URL(request.url()).searchParams.get('after_seq'),
      header: request.headers()['last-event-id'],
    });
    // Drop only the first connection; the next request is served by the actual SSE route.
    if (requests.length === 1) await route.abort('connectionreset');
    else await route.continue();
  });
  const streamed = page.waitForResponse(
    response =>
      response.url().includes('/events/stream?') &&
      response.headers()['content-type']?.startsWith('text/event-stream')
  );
  const task = await submitTask(page);
  expect(writes).toEqual([taskPath]);
  expect((await streamed).status()).toBe(200);
  expect(requests.length).toBeGreaterThanOrEqual(2);
  expect(Number(requests[1].cursor)).toBeGreaterThan(0);
  expect(requests[1].header).toBe(requests[1].cursor);
  await expect(page.getByText('Following supervisor events')).toBeVisible();
  const timeline = page.getByRole('list', { name: 'Saved task events' });
  await expect(timeline.getByText('link_task.completed', { exact: true })).toBeVisible();
  await expect(page.getByText('RESULT SHA-256 VERIFIED')).toBeVisible();
  const seq = await timeline
    .locator('[data-sequence]')
    .evaluateAll(rows => rows.map(row => Number(row.getAttribute('data-sequence'))));
  expect(seq).toEqual([...new Set(seq)].sort((a, b) => a - b));
  await page.reload();
  await expect(page.getByText('Recorded history')).toBeVisible();
  await expect(page.getByText('RESULT SHA-256 VERIFIED')).toBeVisible();
  await expect(timeline.getByText('link_task.completed', { exact: true })).toBeVisible();
  const replay = await page.request.post(taskPath, {
    headers: await authHeaders(page),
    data: task.request,
  });
  expect(replay.status()).toBe(200);
  expect(await replay.json()).toMatchObject({
    task_id: task.id,
    state: 'COMPLETED',
    idempotent_replay: true,
  });
});

test('does not post a slash task if the mapped worker becomes unavailable after review', async ({
  page,
}) => {
  await signIn(page, '/#/ai-workspace');
  await page.locator('main').getByRole('button', { name: '＋ New conversation' }).click();
  await page.getByRole('textbox', { name: 'Message Jarvis' }).fill('/task Read host status');
  await page.getByRole('button', { name: 'Review task', exact: true }).click();
  await pairedDevice(page);
  const submit = page.getByRole('button', { name: 'Submit task', exact: true });
  await expect(submit).toBeEnabled();
  await page.route(`**${readinessPath}`, async route => {
    const response = await route.fetch();
    const report = await response.json();
    expect(report.components.device_mapping.active_device_count).toBeGreaterThan(0);
    await route.fulfill({
      response,
      json: {
        ...report,
        status: 'degraded',
        components: {
          ...report.components,
          supervisor_worker: { state: 'unavailable', reason: 'runner_unavailable' },
        },
      },
    });
  });
  const writes: string[] = [];
  page.on('request', request => {
    if (request.method() === 'POST') writes.push(new URL(request.url()).pathname);
  });
  await submit.click();
  await expect(page.getByText(/Supervisor worker is not ready/)).toBeVisible();
  await expect(submit).toBeDisabled();
  expect(writes).toEqual([]);
});

test('rejects unauthorized task writes and records cancellation and failure from the backend', async ({
  page,
}) => {
  await signIn(page, studio);
  const headers = await authHeaders(page);
  const body = {
    objective: 'Should not be queued.',
    requester_device_id: 'a'.repeat(32),
    capability_scope: ['read_only'],
    idempotency_key: randomUUID(),
  };
  expect(
    (
      await page.request.post(taskPath, { data: body, headers: { Origin: headers.Origin } })
    ).status()
  ).toBe(403);
  const rejected = await page.request.post(taskPath, { data: body, headers });
  expect(rejected.status()).toBe(403);
  expect(await rejected.json()).toMatchObject({
    detail: 'Requester device is not mapped to this Web owner',
  });
  const task = await submitTask(page, 'Fixture slow task');
  const cancellation = page.waitForResponse(response =>
    response.url().endsWith(`/${task.id}/cancel`)
  );
  await page.getByRole('button', { name: 'Cancel task', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm cancellation' }).click();
  expect(await (await cancellation).json()).toMatchObject({
    state: 'CANCELLED',
    cancelled: true,
    action_may_have_started: false,
  });
  await expect(page.getByText('Cancellation recorded.')).toBeVisible();
  await page.reload();
  const detail = page.getByRole('region', { name: 'Supervisor task detail' });
  await expect(detail.getByText('CANCELLED', { exact: true })).toBeVisible();
  await expect(detail.getByText('task.cancelled', { exact: true })).toBeVisible();
  await submitTask(page, 'Fixture supervisor failure');
  await expect(detail.getByRole('alert')).toContainText('Supervisor task failed');
  await expect(detail.getByText('RESULT SHA-256 VERIFIED')).toHaveCount(0);
});

test('reviews the exact operation, rejects digest mismatch, and records explicit decisions', async ({
  page,
}) => {
  await signIn(page, studio);
  const headers = await authHeaders(page);
  for (const choice of ['Reject', 'Approve'] as const) {
    await page.goto(studio);
    const task = await submitTask(page, `Fixture approval: ${choice}`, 'assistant');
    await page.getByRole('link', { name: 'Approvals' }).click();
    await expect(page.getByRole('heading', { name: 'Approvals', exact: true })).toBeVisible();
    const review = page.waitForResponse(response =>
      /\/v1\/web\/approvals\/[a-f0-9]{32}$/.test(response.url())
    );
    await page.getByRole('button', { name: 'Review approval', exact: true }).click();
    const approval = await (await review).json();
    const dialog = page.getByRole('dialog', { name: 'Review exact operation' });
    await expect(dialog).toContainText('mcp:jarvis_mac:click');
    await expect(dialog).toContainText('screen-coordinate');
    await expect(dialog).toContainText('"x": 1');
    await expect(dialog).toContainText(approval.operation_digest);
    await expect(dialog).toContainText(
      `Approval requester device: Isolated test pairing · ${approval.requester_device_id}`
    );
    expect(approval.requester_device_id).toBe(task.request.requester_device_id);
    const decisionPath = `/v1/web/approvals/${approval.id}/decision`;
    if (choice === 'Reject') {
      const stale = await page.request.post(decisionPath, {
        headers,
        data: {
          decision: 'reject',
          task_id: task.id,
          expected_operation_digest: '0'.repeat(64),
          requester_device_id: approval.requester_device_id,
          idempotency_key: randomUUID(),
        },
      });
      expect(stale.status()).toBe(409);
    }
    const decided = page.waitForResponse(response => response.url().endsWith(decisionPath));
    await dialog.getByRole('button', { name: `${choice} this operation` }).click();
    const response = await decided;
    expect(response.status()).toBe(200);
    const decision = response.request().postDataJSON();
    expect(decision).toMatchObject({
      task_id: task.id,
      expected_operation_digest: approval.operation_digest,
      decision: choice.toLowerCase(),
    });
    expect(decision).not.toHaveProperty('continuation_objective');
    const status = choice === 'Reject' ? 'REJECTED' : 'CONSUMED';
    await expect(dialog).toContainText(`Decision response: ${status}`);
    const replay = await page.request.post(decisionPath, { headers, data: decision });
    expect(replay.status()).toBe(200);
    expect(await replay.json()).toMatchObject({ status });
    await dialog.getByRole('button', { name: 'Close Review exact operation' }).click();
    await expect(page.getByText('No pending approvals.')).toBeVisible();
  }
});

test('downloads only verified archives from authorized task endpoints', async ({
  page,
  request,
}) => {
  await signIn(page, studio);
  const archive = page.getByText('Synthetic archive fixture', { exact: true }).locator('../..');
  await archive.getByRole('link', { name: /View task/ }).click();
  const listed = page.waitForResponse(response => response.url().endsWith('/artifacts'));
  await page.getByRole('button', { name: 'Check task artifacts' }).click();
  const list = await listed;
  expect(list.status()).toBe(200);
  const {
    artifacts: [item],
  } = await list.json();
  expect((await request.get(item.download_path)).status()).toBe(401);
  const finished = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download ZIP' }).click();
  const download = await finished;
  expect(download.suggestedFilename()).toBe(item.file_name);
  expect(
    createHash('sha256')
      .update(await readFile((await download.path())!))
      .digest('hex')
  ).toBe(item.archive_sha256);
  await expect(page.getByText(/Archive verified against the server SHA-256/)).toBeVisible();
  await page.route(`**${item.download_path}`, async route => {
    const response = await route.fetch();
    const bytes = await response.body();
    bytes[0] ^= 1;
    await route.fulfill({ response, body: bytes });
  });
  let downloadedCorruptFile = false;
  page.once('download', () => {
    downloadedCorruptFile = true;
  });
  await page.getByRole('button', { name: 'Download ZIP' }).click();
  await expect(page.getByText(/No file was saved/)).toBeVisible();
  expect(downloadedCorruptFile).toBe(false);
});

test('creates and updates a read-only schedule, pauses, resumes, runs and retains history on deletion', async ({
  page,
}) => {
  await signIn(page, '/#/automations');
  await page.getByRole('button', { name: /New schedule/ }).click();
  await page.getByLabel('Read-only objective').fill('Fixture schedule');
  await pairedDevice(page);
  const created = page.waitForResponse(
    response =>
      response.url().endsWith('/v1/web/automations') && response.request().method() === 'POST'
  );
  await page.getByRole('button', { name: 'Create schedule' }).click();
  expect((await created).status()).toBe(202);
  const detail = page.getByRole('heading', { name: 'Schedule details' }).locator('../..');
  await expect(detail.getByText(/Next run:/)).toBeVisible();
  await detail.getByRole('button', { name: 'Edit schedule' }).click();
  await page.getByLabel('Read-only objective').fill('Updated fixture schedule');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(detail.getByText('Updated fixture schedule')).toBeVisible();
  await detail.getByRole('button', { name: 'Pause', exact: true }).click();
  await expect(detail.getByText('PAUSED', { exact: true })).toBeVisible();
  await detail.getByRole('button', { name: 'Resume', exact: true }).click();
  await expect(detail.getByText('ACTIVE', { exact: true })).toBeVisible();
  await detail.getByRole('button', { name: 'Run now' }).click();
  await expect(detail.getByRole('link', { name: 'Follow supervisor task' })).toBeVisible();
  await expect(detail.getByRole('link', { name: 'View supervisor task' })).toBeVisible();
  await expect
    .poll(async () => {
      const href = (await detail
        .getByRole('link', { name: 'Follow supervisor task' })
        .getAttribute('href'))!;
      const task = await page.request.get(
        `${taskPath}/${new URLSearchParams(href.split('?')[1]).get('task')}`
      );
      return (await task.json()).state;
    })
    .toBe('COMPLETED');
  await detail.getByRole('button', { name: 'Refresh schedule and history' }).click();
  await expect(detail.getByText(/Supervisor: COMPLETED/)).toBeVisible();
  await detail.getByRole('button', { name: 'Delete schedule' }).click();
  await page.getByRole('button', { name: 'Confirm deletion' }).click();
  await expect(detail.getByText('CANCELLED', { exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: 'View schedule' }).click();
  await expect(detail.getByText('CANCELLED', { exact: true })).toBeVisible();
  await expect(detail.getByRole('link', { name: 'View supervisor task' })).toBeVisible();
  expect(
    (await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze())
      .violations
  ).toEqual([]);
});

test('shows real mapped pairings and requires a confirmed revocation', async ({ page }) => {
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'AI Workspace' })).toBeVisible();
  await page.getByRole('link', { name: 'Device Hub' }).click();
  await page.getByRole('button', { name: /Revocation test pairing/ }).click();
  await expect(page.getByText('ACTIVE PAIRING', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Revoke device' }).click();
  const confirm = page.getByRole('dialog', { name: 'Revoke paired device?' });
  await expect(confirm.getByRole('button', { name: 'Keep paired' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: 'Revoke device' })).toBeFocused();
  await page.getByRole('button', { name: 'Revoke device' }).click();
  const revoked = page.waitForResponse(response => response.url().endsWith('/revoke'));
  await confirm.getByRole('button', { name: 'Confirm revocation' }).click();
  const response = await revoked;
  expect(response.status()).toBe(200);
  expect(response.request().postDataJSON()).toEqual({ confirm: true });
  const { device_id: removed } = await response.json();
  await expect(page.getByText(/Device pairing revoked/)).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: /Isolated test pairing/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /Revocation test pairing/ })).toHaveCount(0);
  expect((await page.request.get(`/v1/web/devices/${removed}`)).status()).toBe(404);
});
