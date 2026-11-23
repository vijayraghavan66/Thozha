import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { mkdir, writeFile, rm } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { signIn } from './session';

async function createConversation(page: Page, title: string) {
  const session = await (await page.request.get('/v1/web/session')).json();
  const response = await page.request.post('/v1/web/conversations', {
    headers: { Origin: new URL(page.url()).origin, 'X-Thozha-CSRF': session.csrf_token },
    data: { title },
  });
  expect(response.ok()).toBeTruthy();
  const { id } = await response.json();
  await page.goto(`/#/ai-workspace?conversation=${id}`);
  await page.reload();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Message Jarvis' })).toBeEditable();
}

test('sidebar actions survive reload and protect other conversations', async ({
  page,
}, testInfo) => {
  await signIn(page);
  const keep = 'Sidebar regression — keep';
  const target =
    'Sidebar regression — archive then delete a conversation with a deliberately long title';
  await createConversation(page, keep);
  await createConversation(page, target);
  const history = page.getByRole('region', { name: 'Conversation history' });
  const trigger = history.getByRole('button', { name: `Actions for ${target}` });
  const assetDir = process.env.THOZHA_E2E_43_CAPTURE;
  if (assetDir) {
    expect(await page.evaluate(() => document.visibilityState)).toBe('visible');
    await page.screencast.start({
      path: join(assetDir, 'conversation-actions.webm'),
      size: { width: 1200, height: 800 },
    });
  }
  try {
    await trigger.focus();
    const pill = trigger.locator('..');
    const before = await pill.boundingBox();
    const neighbor = history.getByRole('link', { name: keep, exact: true });
    const neighborBefore = await neighbor.boundingBox();
    await trigger.press('Enter');
    const actions = history.getByRole('group', { name: 'Conversation actions' });
    const close = history.getByRole('button', { name: `Close actions for ${target}` });
    await expect(actions).toBeVisible();
    await expect(history.getByRole('link', { name: target, exact: true })).toHaveCount(0);
    expect(await close.locator('..').boundingBox()).toEqual(before);
    expect(await neighbor.boundingBox()).toEqual(neighborBefore);
    if (assetDir) {
      await expect.poll(() => actions.evaluate(el => getComputedStyle(el).opacity)).toBe('1');
      await page.screenshot({ path: join(assetDir, 'desktop-pill.png') });
      await page.waitForTimeout(650);
    }
    await expect(history.getByRole('button', { name: 'Archive', exact: true })).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(actions.getByRole('button', { name: 'Delete', exact: true })).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(trigger).toBeFocused();
    await expect(trigger).toHaveAttribute('aria-expanded', 'false');
    await trigger.click();
    await close.click();
    await expect(actions).toHaveCount(0);
    expect(await pill.boundingBox()).toEqual(before);
    await trigger.click();
    await history.getByText('SAVED CONVERSATIONS', { exact: true }).click();
    await expect(actions).toHaveCount(0);
    await trigger.press('Enter');
    await history.getByRole('button', { name: 'Delete', exact: true }).click();
    const confirmation = page.getByRole('dialog', { name: 'Delete conversation' });
    await expect(confirmation).toContainText(target);
    await expect(confirmation.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused();
    await confirmation.getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(trigger).toBeFocused();

    await trigger.click();
    await history.getByRole('button', { name: 'Archive', exact: true }).click();
    if (assetDir)
      await page
        .getByRole('dialog', { name: 'Archive conversation' })
        .screenshot({ path: join(assetDir, 'archive-confirmation.png') });
    await page.getByRole('button', { name: 'Archive conversation', exact: true }).click();
    await expect(page.getByRole('heading', { name: keep, exact: true })).toBeVisible();
    await expect(history.getByRole('link', { name: target })).toHaveCount(0);
    await history.getByRole('button', { name: 'Show archived conversations' }).click();
    await history.getByRole('link', { name: target }).click();
    await page.reload();
    await expect(page.getByRole('heading', { name: target, exact: true })).toBeVisible();
    await expect(page.getByText('Archived · read-only')).toBeVisible();
    await history.getByRole('button', { name: 'Show archived conversations' }).click();
    await trigger.click();
    await history.getByRole('button', { name: 'Delete', exact: true }).click();
    let writes = 0;
    const detail = '**/v1/web/conversations/*';
    await page.route(detail, async route => {
      if (route.request().method() !== 'DELETE') return route.continue();
      writes++;
      if (writes === 1) return route.fulfill({ status: 503, json: { detail: 'Injected outage' } });
      // Commit on the fixture server, then drop the response to exercise reconciliation.
      const response = await route.fetch();
      expect(response.status()).toBe(204);
      await route.abort('failed');
    });
    if (assetDir) {
      await confirmation.screenshot({ path: join(assetDir, 'delete-confirmation.png') });
      await page.waitForTimeout(650);
    }
    expect(
      (await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze())
        .violations
    ).toEqual([]);
    await confirmation.getByRole('button', { name: 'Delete permanently' }).click();
    await expect(confirmation.getByRole('alert')).toBeVisible();
    // Duplicate form submission while the mutation is in flight is ignored.
    await confirmation.locator('form').evaluate(form => {
      if (!(form instanceof HTMLFormElement)) throw new Error('Missing confirmation form');
      form.requestSubmit();
      form.requestSubmit();
    });
    await expect(confirmation).toHaveCount(0);
    expect(writes).toBe(2);
    await expect(history.getByRole('link', { name: target })).toHaveCount(0);
    await expect(history.getByRole('button', { name: 'Show active conversations' })).toBeFocused();
    await page.reload();
    await expect(history.getByRole('link', { name: keep })).toBeVisible();
    await history.getByRole('button', { name: 'Show archived conversations' }).click();
    await expect(history.getByRole('link', { name: target })).toHaveCount(0);
    await testInfo.attach('sidebar-actions', {
      body: await page.screenshot(),
      contentType: 'image/png',
    });
  } finally {
    if (assetDir) await page.screencast.stop();
  }
  await history.getByRole('button', { name: 'Show active conversations' }).click();
  await history.getByRole('button', { name: `Actions for ${keep}` }).click();
  await history.getByRole('button', { name: 'Delete', exact: true }).click();
  await page.unroute('**/v1/web/conversations/*');
  await page.getByRole('button', { name: 'Delete permanently' }).click();
  await expect(history.getByRole('link', { name: keep })).toHaveCount(0);
});

test('touch drawer opens saved conversation actions and reconciles an already-deleted record', async ({
  browser,
  baseURL,
}) => {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
  });
  try {
    const page = await context.newPage();
    await signIn(page, '/#/settings');
    const title = 'Touch drawer regression';
    await createConversation(page, title);
    await page.getByRole('button', { name: 'Open navigation' }).tap();
    const drawer = page.getByRole('dialog', { name: 'Workspace navigation' });
    const trigger = drawer.getByRole('button', { name: `Actions for ${title}` });
    await trigger.scrollIntoViewIfNeeded();
    await expect
      .poll(() =>
        drawer.evaluate(el =>
          el.getAnimations().some(animation => animation.playState === 'running')
        )
      )
      .toBe(false);
    const bounds = await trigger.locator('..').boundingBox();
    await trigger.tap();
    const close = drawer.getByRole('button', { name: `Close actions for ${title}` });
    expect(await close.locator('..').boundingBox()).toEqual(bounds);
    const [archive, remove, dismiss] = await Promise.all(
      [
        drawer.getByRole('button', { name: 'Archive', exact: true }),
        drawer.getByRole('button', { name: 'Delete', exact: true }),
        close,
      ].map(button => button.boundingBox())
    );
    if (!archive || !remove || !dismiss) throw new Error('Missing touch actions');
    expect(archive.x + archive.width).toBeLessThanOrEqual(remove.x);
    expect(remove.x + remove.width).toBeLessThanOrEqual(dismiss.x);
    for (const target of [archive, remove, dismiss]) {
      expect(Math.round(target.width * 100) / 100).toBeGreaterThanOrEqual(44);
      expect(Math.round(target.height * 100) / 100).toBeGreaterThanOrEqual(44);
    }
    const assetDir = process.env.THOZHA_E2E_43_CAPTURE;
    if (assetDir)
      await page.screenshot({ path: join(assetDir, 'mobile-pill.png'), animations: 'disabled' });
    await drawer.getByRole('button', { name: 'Delete', exact: true }).tap();
    const confirmation = page.getByRole('dialog', { name: 'Delete conversation' });
    await expect(confirmation).toContainText(title);
    const session = await (await page.request.get('/v1/web/session')).json();
    const id = new URLSearchParams(new URL(page.url()).hash.split('?')[1]).get('conversation');
    const concurrent = await page.request.delete(`/v1/web/conversations/${id}`, {
      headers: {
        Origin: new URL(page.url()).origin,
        'X-Thozha-CSRF': session.csrf_token,
        'Content-Type': 'application/json',
      },
    });
    expect(concurrent.status()).toBe(204);
    await confirmation.getByRole('button', { name: 'Delete permanently' }).tap();
    await expect(drawer.getByRole('status')).toHaveText('Conversation is no longer on the server.');
    await expect(drawer.getByRole('link', { name: title })).toHaveCount(0);
  } finally {
    await context.close();
  }
});

test('startup waits for real session verification, settles and never replays on navigation', async ({
  page,
}) => {
  await signIn(page, '/#/ai-workspace?new=1');
  await expect(page.locator('.th-startup')).toHaveCount(0);
  let release!: () => void;
  const held = new Promise<void>(resolve => {
    release = resolve;
  });
  const endpoint = '**/v1/web/session';
  await page.route(endpoint, async route => {
    await held;
    await route.continue();
  });
  const assetDir = process.env.THOZHA_E2E_43_CAPTURE;
  if (assetDir) {
    expect(await page.evaluate(() => document.visibilityState)).toBe('visible');
    await page.screencast.start({
      path: join(assetDir, 'living-pulse-startup.webm'),
      size: { width: 1200, height: 800 },
    });
  }
  try {
    await page.reload({ waitUntil: 'domcontentloaded' });
    const startup = page.locator('.th-startup');
    await expect(page.getByRole('status')).toHaveText('Checking browser session…');
    await expect(page.locator('.th-v4')).toHaveCount(0);
    await expect
      .poll(() =>
        startup
          .locator('.th-pulse-primary')
          .evaluate(element =>
            element
              .getAnimations({ subtree: true })
              .some(animation => animation.playState === 'running')
          )
      )
      .toBe(true);
    await expect
      .poll(() =>
        startup
          .locator('.th-pulse-primary')
          .evaluate(element =>
            element
              .getAnimations({ subtree: true })
              .some(animation => animation.playState === 'running')
          )
      )
      .toBe(false);
    if (assetDir) await page.screenshot({ path: join(assetDir, 'living-pulse-startup.png') });
    expect(
      (await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze())
        .violations
    ).toEqual([]);
    release();
    await expect(page.locator('[data-pulse-in-flight]')).toHaveCount(1);
    expect(await startup.evaluate(el => getComputedStyle(el).pointerEvents)).toBe('none');
    await expect(startup).toHaveCount(0);
    await expect(page.locator('[data-pulse-slot]:visible')).toHaveCount(1);
    await expect(page.getByTestId('chat-title')).toHaveText('New chat');
    await page.getByRole('link', { name: 'Settings', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Settings', exact: true }).last()).toBeVisible();
    const requests: string[] = [];
    const record = (request: import('@playwright/test').Request) => requests.push(request.url());
    // Let the Settings route settle before measuring the entirely local preview.
    await page.waitForLoadState('networkidle');
    page.on('request', record);
    const replay = page.getByRole('button', { name: 'Replay introduction' });
    await replay.click();
    await expect(page.locator('[data-pulse-in-flight]')).toHaveCount(1);
    await expect(startup).toHaveCount(0);
    await expect(replay).toBeFocused();
    await replay.click();
    await expect(startup).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(startup).toHaveCount(0);
    await expect(replay).toBeFocused();
    await replay.click();
    await expect(startup).toBeVisible();
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(startup).toHaveCount(0);
    await expect(page.locator('[data-pulse-in-flight]')).toHaveCount(0);
    page.off('request', record);
    expect(requests).toEqual([]);
    await expect(startup).toHaveCount(0);
  } finally {
    release();
    if (assetDir) await page.screencast.stop();
  }
});

for (const motion of ['system', 'application'] as const) {
  test(`startup is static with ${motion} reduced motion and exits to retry on outage`, async ({
    page,
  }) => {
    if (motion === 'system') await page.emulateMedia({ reducedMotion: 'reduce' });
    else await page.addInitScript(() => localStorage.setItem('thozha-web.motion.v1', 'reduce'));
    let release!: () => void;
    const held = new Promise<void>(resolve => {
      release = resolve;
    });
    const endpoint = '**/v1/web/session';
    const outage = async (route: import('@playwright/test').Route) => {
      await held;
      await route.abort('failed');
    };
    await page.route(endpoint, outage);
    try {
      await page.goto('/', { waitUntil: 'domcontentloaded' });
      const startup = page.locator('.th-startup-wait');
      await expect(startup).toBeVisible();
      expect(
        await startup.evaluate(element => element.getAnimations({ subtree: true }).length)
      ).toBe(0);
    } finally {
      release();
    }
    await expect(page.getByRole('alert')).toContainText('could not reach');
    await expect(page.locator('.th-startup')).toHaveCount(0);
    await page.unroute(endpoint, outage);
    await page.getByRole('button', { name: 'Retry', exact: true }).click();
    await expect(page.getByLabel('Workspace ID')).toBeVisible();
    await expect(page.locator('.th-startup')).toHaveCount(0);
  });
}

async function send(page: Page, content: string) {
  await page.getByRole('textbox', { name: 'Message Jarvis' }).fill(content);
  await page.getByRole('button', { name: /Send ↑/ }).click();
}

test('mobile slash commands stay local and refuse tasks without verified readiness', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page, '/#/ai-workspace?new=1');
  await expect(page.locator('.th-startup')).toHaveCount(0);
  const writes: string[] = [];
  page.on('request', request => {
    if (request.method() === 'POST') writes.push(new URL(request.url()).pathname);
  });
  const composer = page.getByRole('textbox', { name: 'Message Jarvis' });
  const palette = page.getByRole('listbox', { name: 'Composer commands' });
  await composer.fill('/');
  await expect(palette.getByRole('option')).toHaveCount(3);
  expect(
    (await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze())
      .violations
  ).toEqual([]);
  await composer.press('ArrowDown');
  await expect(palette.getByRole('option', { selected: true })).toContainText('/status');
  await composer.press('Escape');
  await expect(palette).toHaveCount(0);
  await composer.fill('/he');
  await composer.press('Tab');
  await expect(composer).toHaveValue('/help ');
  await expect(composer).toBeFocused();
  await composer.press('Enter');
  const help = page.getByRole('dialog', { name: 'Composer commands' });
  await expect(help).toContainText('/research');
  await expect(help).toContainText('Not enabled');
  await page.keyboard.press('Escape');
  await expect(composer).toBeFocused();
  await composer.fill('/st');
  await palette.getByRole('option', { name: /\/status/ }).click();
  await composer.press('Enter');
  const status = page.getByRole('dialog', { name: 'Host readiness' });
  await expect(status).toContainText('Host reports: degraded');
  await expect(status).toContainText('No active device mapping was verified');
  await page.keyboard.press('Escape');
  await composer.fill('/not-a-command');
  await composer.press('Enter');
  await expect(page.getByRole('alert')).toContainText('Unknown command');
  await composer.fill('/task Read host status');
  await composer.press('Enter');
  await expect(page.getByLabel('What should Thozha do?')).toHaveValue('Read host status');
  await expect(page.getByText(/No active device mapping was verified/)).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Submit reviewed task', exact: true })
  ).toBeDisabled();
  expect(writes).toEqual([]);
});

test('private screens require the real cookie session and sign-out revokes it', async ({
  page,
  context,
}) => {
  const privateReads: string[] = [];
  page.on('request', request => {
    if (request.url().includes('/v1/web/conversations')) privateReads.push(request.url());
  });
  await page.goto('/#/ai-workspace');
  await expect(page.getByRole('heading', { name: 'Sign in to Thozha' })).toBeVisible();
  await page.getByLabel('Workspace ID').fill('web-e2e-owner');
  await page.getByLabel('Password').fill('incorrect-test-password');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('incorrect');
  expect(privateReads).toEqual([]);

  await signIn(page, '/#/settings');
  const sessionCookie = (await context.cookies()).find(
    cookie => cookie.name === 'thozha_web_session'
  );
  expect(sessionCookie?.httpOnly).toBe(true);
  expect(sessionCookie?.sameSite).toBe('Strict');
  expect(sessionCookie?.path).toBe('/v1/web');
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
  await context.setOffline(true);
  await expect(page.getByRole('alert')).toContainText('You are offline');
  await context.setOffline(false);
  await expect(page.getByText('SESSION VERIFIED')).toBeVisible();
  const logout = page.waitForResponse(response =>
    response.url().endsWith('/v1/web/session/logout')
  );
  await page.getByRole('button', { name: 'Sign out' }).click();
  expect((await logout).status()).toBe(204);
  await expect(page.getByLabel('Password')).toBeVisible();
  expect((await page.request.get('/v1/web/session')).status()).toBe(401);
  expect((await page.request.get('/v1/web/conversations')).status()).toBe(401);
});

test('renders the reported SSE mode, persists context, copies code and supports archiving/deletion', async ({
  page,
  context,
}) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await signIn(page, '/#/ai-workspace');
  await createConversation(page, 'Browser history verification');
  const stream = page.waitForResponse(response => response.url().endsWith('/messages/stream'));
  await send(page, 'Remember the name Thozha Alpha for context.');
  expect((await stream).headers()['content-type']).toContain('text/event-stream');
  if (process.env.THOZHA_E2E_MODE === 'buffered') {
    await expect(
      page.getByText('Buffered response · waiting for the complete result.')
    ).toBeVisible();
    await expect(page.getByLabel('Partial response')).toHaveCount(0);
  } else {
    await expect(page.getByLabel('Partial response')).toContainText('Fixture response');
  }
  await expect(page.getByRole('heading', { name: 'Fixture response' })).toBeVisible();
  const copy = page.locator('.th-code-copy');
  const bounds = await copy.boundingBox();
  await copy.focus();
  await page.keyboard.press('Enter');
  await expect(copy).toHaveAccessibleName('Copied!');
  await expect(copy.locator('[data-copy-label="copied"]')).toHaveCSS('opacity', '1');
  await expect(copy).toBeFocused();
  expect(await copy.boundingBox()).toEqual(bounds);
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    "const workspace = 'Thozha Alpha';"
  );
  await expect(copy).toHaveAccessibleName('Copy code');
  await page.reload();
  await expect(
    page.getByText('Remember the name Thozha Alpha for context.', { exact: true })
  ).toBeVisible();
  await send(page, 'check prior context');
  await expect(page.getByText('Context found.', { exact: true })).toBeVisible();
  await page.getByText(/Response events \(/).click();
  await expect(page.getByText('Response completed', { exact: true }).last()).toBeVisible();
  await page.getByRole('button', { name: 'Archive', exact: true }).click();
  await page.getByRole('button', { name: 'Archive conversation', exact: true }).click();
  await expect(page.getByText('Conversation archived.')).toBeVisible();
  const history = page.getByRole('complementary', { name: 'Workspace sidebar' });
  await history.getByRole('button', { name: 'Show archived conversations' }).click();
  await history.getByRole('link', { name: 'Browser history verification' }).click();
  await expect(page.getByRole('heading', { name: 'Browser history verification' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Message Jarvis' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  await page.getByRole('button', { name: 'Delete permanently' }).click();
  await expect(page.getByText('Conversation and history deleted.')).toBeVisible();
  await expect(
    page
      .getByRole('region', { name: 'Conversation messages' })
      .getByText('No archived conversations.')
  ).toBeVisible();
});

test('code copy shows touch-friendly failure and retry without moving the block', async ({
  browser,
  baseURL,
}, info) => {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    permissions: ['clipboard-read', 'clipboard-write'],
    reducedMotion: 'reduce',
  });
  try {
    const page = await context.newPage();
    await page.addInitScript(() => {
      const write = Clipboard.prototype.writeText;
      let deny = true;
      Clipboard.prototype.writeText = function (text) {
        if (!deny) return write.call(this, text);
        deny = false;
        return Promise.reject(
          new DOMException('Fixture denied clipboard access', 'NotAllowedError')
        );
      };
    });
    await signIn(page);
    await createConversation(page, 'Clipboard feedback');
    await send(page, 'Show a test response with a code block.');
    const copy = page.getByRole('button', { name: 'Copy code' });
    await expect(copy).toBeVisible();
    const block = page.getByRole('region', { name: 'Conversation messages' }).locator('pre');
    const bounds = await copy.boundingBox();
    const blockBounds = await block.boundingBox();
    await copy.screenshot({ path: info.outputPath('copy-default.png') });
    await copy.tap();
    const retry = page.getByRole('button', { name: "Couldn't copy" });
    await expect(retry).toBeVisible();
    await expect(retry.locator('[data-copy-label="failed"]')).toHaveCSS('opacity', '1');
    await expect(retry.locator('[data-copy-label="failed"]')).toHaveCSS(
      'transition-duration',
      '0s'
    );
    expect(await retry.boundingBox()).toEqual(bounds);
    await retry.screenshot({ path: info.outputPath('copy-failed.png') });
    await retry.tap();
    const copied = page.getByRole('button', { name: 'Copied!' });
    await expect(copied).toBeVisible();
    expect(await copied.boundingBox()).toEqual(bounds);
    expect(await block.boundingBox()).toEqual(blockBounds);
    await copied.screenshot({ path: info.outputPath('copy-success.png') });
  } finally {
    await context.close();
  }
});

test('persists actual cancellation and exposes engine failure and prompt recovery', async ({
  page,
}) => {
  await signIn(page, '/#/ai-workspace');
  await createConversation(page, 'Cancellation verification');
  await send(page, 'Check cancellation behavior');
  await page.getByRole('button', { name: 'Stop generating' }).click();
  await expect(page.getByText('Response cancelled.', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Fixture response' })).toHaveCount(0);
  await page.getByText(/Response events \(/).click();
  await expect(page.getByText('Cancellation requested', { exact: true })).toBeVisible();
  await send(page, 'Check generation failure');
  await expect(
    page.getByText('The configured model could not complete the response.')
  ).toBeVisible();
  await page.getByRole('button', { name: 'Reuse prompt' }).click();
  await expect(page.getByRole('textbox', { name: 'Message Jarvis' })).toHaveValue(
    'Check generation failure'
  );
});

test('uses the real 202 API and persisted polling when the stream route is unavailable', async ({
  page,
}) => {
  await page.route('**/messages/stream', route => route.fulfill({ status: 405 }));
  await signIn(page, '/#/ai-workspace');
  await createConversation(page, 'Snapshot fallback verification');
  const accepted = page.waitForResponse(response => response.url().endsWith('/messages'));
  await send(page, 'Check the snapshot fallback');
  expect((await accepted).status()).toBe(202);
  await expect(page.getByText('Generating response…')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Fixture response' })).toBeVisible();
  await expect(page.getByLabel('Partial response')).toHaveCount(0);
});

test('recovers a lost acceptance using the same key without duplicating history', async ({
  page,
}) => {
  await signIn(page, '/#/ai-workspace');
  await createConversation(page, 'Idempotent recovery verification');
  let key: string | undefined;
  await page.route('**/messages/stream', async route => {
    key = route.request().headers()['idempotency-key'];
    await route.fetch();
    await route.abort('failed');
  });
  await send(page, 'Persist exactly one prompt');
  const retry = page.getByRole('button', { name: 'Retry send safely' });
  await expect(retry).toBeVisible();
  const receipt = page.waitForResponse(response => response.url().endsWith('/messages'));
  await retry.click();
  const response = await receipt;
  expect(response.request().headers()['idempotency-key']).toBe(key);
  expect(await response.json()).toMatchObject({ replayed: true });
  await expect(page.getByText('Persist exactly one prompt', { exact: true })).toHaveCount(1);
});

test('creates, searches, reads, and deletes real owner-scoped memory', async ({ page }) => {
  await signIn(page, '/#/memory-center');
  await page.getByLabel('Title (optional)').fill('Editor preference test');
  await page.getByLabel('Detail', { exact: true }).fill('Prefer a quiet editor theme.');
  await page.getByLabel('Tags (comma-separated)').fill('editor, theme');
  await page.getByRole('button', { name: 'Save on Thozha host' }).click();
  await expect(page.getByText('Saved to this owner’s Web memory.')).toBeVisible();
  await page.reload();
  await page.getByLabel('Search saved memories').fill('QUIET');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await page.getByRole('button', { name: /Editor preference test/ }).click();
  await expect(
    page.getByRole('dialog', { name: 'Read memory' }).getByText('Prefer a quiet editor theme.')
  ).toBeVisible();
  await page.getByRole('button', { name: 'Delete memory', exact: true }).click();
  await page.getByRole('button', { name: 'Delete permanently' }).click();
  await expect(page.getByText('Memory deleted from the host.')).toBeVisible();
  await expect(page.getByText('No matching memories.')).toBeVisible();
});

test('works at mobile width with keyboard navigation and honest local previews', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await signIn(page);
  await expect(page.getByTestId('chat-title')).toBeVisible();
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('link', { name: 'Trading Lab' }).click();
  await expect(page.getByRole('heading', { name: 'Trading Lab' })).toBeVisible();
  await expect(page.getByText('SYNTHETIC DEMO', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Load synthetic demo' }).click();
  await expect(page.getByText('SYNTHETIC DEMO', { exact: true })).toBeVisible();
  await expect(page.locator('.th-market-chart canvas').first()).toBeVisible();
  await page.getByRole('button', { name: 'Place virtual buy' }).click();
  await expect(page.getByRole('cell', { name: 'Buy / Market' })).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: 'Load synthetic demo' }).click();
  await expect(page.getByRole('cell', { name: 'Buy / Market' })).toBeVisible();
  await page.keyboard.press('Control+k');
  await page.getByRole('combobox', { name: 'Jump to page' }).fill('automations');
  await page.keyboard.press('Enter');
  await page.getByRole('button', { name: /New draft/ }).click();
  await page.getByLabel('Routine name').fill('On-page draft');
  await page.getByRole('button', { name: 'Keep draft on this page' }).click();
  await expect(page.getByText('LOCAL DRAFT')).toBeVisible();
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('link', { name: 'Device Hub' }).click();
  await page.getByRole('button', { name: 'Explore sample devices' }).click();
  await page.getByRole('button', { name: /Smart speaker/ }).click();
  await expect(page.getByRole('button', { name: 'Pair device' })).toBeDisabled();
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('link', { name: 'Developer Studio' }).click();
  await page.getByRole('button', { name: 'Review & submit' }).click();
  await expect(page.getByText(/No active device mapping was verified/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Submit reviewed task' })).toBeDisabled();
  expect((await page.request.get('/v1/web/tasks')).status()).toBe(403);
  expect((await page.request.get('/v1/web/automations/capabilities')).status()).toBe(404);
  await page.getByTestId('search-trigger').click();
  await expect(page.getByRole('combobox', { name: 'Jump to page' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('search-trigger')).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
});

test('header search morphs in place, supports keyboard selection and closes predictably', async ({
  page,
}) => {
  await signIn(page);
  const trigger = page.getByTestId('search-trigger');
  const initialBox = await trigger.boundingBox();
  const field = page.locator('.th-search-field');
  const collapsed = await field.evaluate(el => getComputedStyle(el).clipPath);
  const fieldBox = await field.boundingBox();
  expect(initialBox).not.toBeNull();
  await page.keyboard.press('Control+k');
  const input = page.getByRole('combobox', { name: 'Jump to page' });
  await expect(input).toBeFocused();
  await expect(trigger).toHaveAttribute('aria-label', 'Close search');
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByRole('listbox', { name: 'Workspace pages' })).toBeVisible();
  expect(
    (await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze())
      .violations
  ).toEqual([]);
  await expect
    .poll(() => field.evaluate(el => getComputedStyle(el).clipPath))
    .toBe('inset(0px round 12px)');
  expect(await field.boundingBox()).toEqual(fieldBox);
  const [inputBox, expandedTriggerBox] = await Promise.all([
    input.boundingBox(),
    trigger.boundingBox(),
  ]);
  expect(inputBox).not.toBeNull();
  expect(expandedTriggerBox).not.toBeNull();
  expect(inputBox!.x).toBeLessThan(expandedTriggerBox!.x);
  expect(
    Math.abs(inputBox!.x + inputBox!.width - (expandedTriggerBox!.x + expandedTriggerBox!.width))
  ).toBeLessThanOrEqual(1);
  expect(expandedTriggerBox).toEqual(initialBox);

  await input.fill('memory');
  await expect(page.getByRole('option', { name: /Memory Center/ })).toBeVisible();
  await page.getByRole('button', { name: 'Clear search' }).click();
  await expect(input).toHaveValue('');
  await expect(input).toBeFocused();
  await input.press('ArrowDown');
  await expect(input).toHaveAttribute('aria-activedescendant', 'workspace-search-option-tasks');
  await input.press('Enter');
  await expect(page.getByRole('heading', { name: 'Tasks', exact: true })).toBeVisible();
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');

  await trigger.click();
  await expect(input).toBeFocused();
  await input.press('Escape');
  await expect(trigger).toBeFocused();
  await expect(trigger).toHaveAttribute('aria-label', 'Search pages');
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  expect(await trigger.boundingBox()).toEqual(initialBox);
  await expect.poll(() => field.evaluate(el => getComputedStyle(el).clipPath)).toBe(collapsed);

  await trigger.click();
  await page.mouse.click(100, 360);
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
});

test('conversation-first navigation and contextual inspector remain reversible and keyboard accessible', async ({
  page,
}) => {
  await signIn(page);
  await expect(page.getByTestId('chat-title')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'A quieter kind of control.' })).toHaveCount(0);
  await createConversation(page, 'Sidebar history item');
  await page.getByRole('link', { name: 'Tasks', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Tasks', exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Sidebar history item' }).click();
  await expect(page.getByRole('heading', { name: 'Sidebar history item' })).toBeVisible();

  const rail = page.getByRole('complementary', { name: 'Workspace sidebar' });
  await expect(rail).toHaveCSS('width', '252px');
  await page.getByRole('button', { name: 'Collapse sidebar' }).click();
  await expect(rail).toHaveCSS('width', '68px');
  await page.getByRole('button', { name: 'Expand sidebar' }).click();
  await expect(rail).toHaveCSS('width', '252px');

  await page.getByRole('button', { name: 'Close context panel' }).first().click();
  await expect(page.getByRole('complementary', { name: 'Context inspector' })).toHaveAttribute(
    'inert',
    ''
  );
  await page.getByRole('button', { name: 'Open context panel' }).click();
  await expect(page.getByRole('button', { name: 'Review as supervisor task' })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Open context panel' }).click();
  const sheet = page.getByRole('dialog', { name: 'Context inspector' });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByRole('button', { name: 'Close context panel' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: 'Open context panel' })).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
});

test('new conversation from another section waits for the real chat module and keeps server history', async ({
  page,
}) => {
  await signIn(page, '/#/settings');
  await page
    .getByRole('complementary', { name: 'Workspace sidebar' })
    .getByRole('button', { name: 'New conversation' })
    .click();
  await expect(page.getByRole('heading', { name: 'New chat', exact: true })).toBeVisible();
  await expect(page.getByRole('dialog', { name: 'New conversation' })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: 'Message Jarvis' })).toBeFocused();
  const writes: string[] = [];
  page.on('request', request => {
    if (request.method() === 'POST') writes.push(new URL(request.url()).pathname);
  });
  const composer = page.getByRole('textbox', { name: 'Message Jarvis' });
  const firstMessage = 'Check my Mac files and write a summary';
  await composer.fill(firstMessage);
  await composer.press('Enter');
  await composer.press('Enter');
  await expect(page.getByRole('heading', { name: firstMessage })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Fixture response' })).toBeVisible();
  expect(writes).toEqual(['/v1/web/conversations', expect.stringMatching(/\/messages\/stream$/)]);
  await page.reload();
  await expect(page.getByRole('link', { name: firstMessage })).toBeVisible();
  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  await page.getByRole('button', { name: 'Delete permanently' }).click();
});

test('mobile menu morphs into a close state and its drawer closes outside or with Escape', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  const trigger = page.getByRole('button', { name: 'Open navigation' });
  const initialBox = await trigger.boundingBox();
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  await trigger.click();
  const dialog = page.getByRole('dialog', { name: 'Workspace navigation' });
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveCSS('animation-name', 'th-drawer-in');
  expect(
    (await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze())
      .violations
  ).toEqual([]);
  await expect(page.getByTestId('navigation-trigger')).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('.th-menu-top')).toHaveClass(/is-open/);
  await expect(dialog.getByRole('link', { name: 'AI Workspace' })).toBeFocused();
  await page.mouse.click(370, 420);
  await expect(dialog).toHaveClass(/is-closing/);
  await expect(dialog).toHaveCSS('animation-name', 'th-drawer-out');
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId('navigation-trigger')).toBeFocused();
  expect(await page.getByTestId('navigation-trigger').boundingBox()).toEqual(initialBox);
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveClass(/is-closing/);
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId('navigation-trigger')).toBeFocused();
});

test('notifications unfold from the fixed bell and reverse without trapping focus', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  const bell = page
    .locator('header button[aria-controls]')
    .filter({ has: page.locator('.th-bell') });
  const box = await bell.boundingBox();
  const panel = page.locator('.th-notification-surface');
  const heading = page.getByRole('heading', { name: 'Notifications', exact: true });
  const collapsed = await panel.evaluate(el => getComputedStyle(el).clipPath);
  await bell.click();
  await expect(heading).toBeFocused();
  await expect(bell).toHaveAttribute('aria-expanded', 'true');
  await expect.poll(() => panel.evaluate(el => getComputedStyle(el).opacity)).toBe('1');
  expect(await bell.boundingBox()).toEqual(box);
  expect((await panel.boundingBox())!.x).toBeGreaterThan(0);
  expect(await page.getByRole('dialog').count()).toBe(0);
  await page.keyboard.press('Escape');
  await expect(bell).toBeFocused();
  await expect(heading).toHaveCount(0);
  await expect.poll(() => panel.evaluate(el => getComputedStyle(el).clipPath)).toBe(collapsed);
  await bell.click();
  await page.mouse.click(30, 490);
  await expect(bell).toHaveAttribute('aria-expanded', 'false');
});

for (const variant of ['desktop', 'mobile', 'reduced'] as const) {
  test(`living workspace keeps one composer from welcome to transcript (${variant})`, async ({
    page,
  }) => {
    const mobile = variant === 'mobile';
    const size = mobile ? { width: 390, height: 844 } : { width: 1280, height: 880 };
    await page.setViewportSize(size);
    await page.emulateMedia({ reducedMotion: variant === 'reduced' ? 'reduce' : 'no-preference' });
    await signIn(page, '/#/ai-workspace?new=1');
    await expect(page.locator('.th-startup')).toHaveCount(0);
    const assetDir = process.env.THOZHA_E2E_45_CAPTURE;
    if (assetDir) {
      expect(await page.evaluate(() => document.visibilityState)).toBe('visible');
      await page.screencast.start({ path: join(assetDir, `${variant}.webm`), size });
    }
    try {
      await page.addInitScript(() => {
        const frames: number[] = [];
        const start = performance.now();
        let previous = start;
        function sample(now: number) {
          frames.push(now - previous);
          previous = now;
          if (now - start < 1400) requestAnimationFrame(sample);
          else performance.mark('launch-frame-samples', { detail: frames.slice(1) });
        }
        requestAnimationFrame(sample);
      });
      await page.reload();
      const startup = page.locator('.th-startup');
      const composer = page.getByRole('textbox', { name: 'Message Jarvis' });
      await expect(composer).toBeEditable();
      if (variant === 'reduced') {
        await expect(startup).toHaveCount(0);
        expect(
          await page.locator('.th-v4-welcome-mark svg').evaluate(el => el.getAnimations())
        ).toEqual([]);
      } else {
        await expect(page.locator('[data-pulse-in-flight]')).toHaveCount(1);
        expect(await startup.evaluate(el => getComputedStyle(el).pointerEvents)).toBe('none');
        await expect(startup).toHaveCount(0);
      }
      const greeting = page.getByRole('heading', { name: "What's on your mind?" });
      await expect(greeting).toBeVisible();
      await expect(page.getByRole('heading', { level: 1 })).toHaveText('New chat');
      await expect(
        page.getByText(/CONVERSATIONS \/ WEB v1|Host conversation · delivery/)
      ).toHaveCount(0);
      await expect(page.locator('main canvas')).toHaveCount(0);
      await expect(
        page.locator('main').getByRole('button', { name: /New conversation/i })
      ).toHaveCount(0);
      expect(await page.evaluate(() => [scrollY, document.documentElement.scrollWidth])).toEqual([
        0,
        size.width,
      ]);
      if (assetDir) await page.screenshot({ path: join(assetDir, `${variant}-welcome.png`) });
      const requests: string[] = [];
      page.on('request', request => {
        if (request.method() === 'POST') requests.push(new URL(request.url()).pathname);
      });
      await page.getByRole('button', { name: 'Start a task' }).click();
      await expect(composer).toHaveValue('/task ');
      await expect(composer).toBeFocused();
      await expect(page.getByRole('button', { name: 'Review task' })).toBeVisible();
      expect(requests).toEqual([]);
      await composer.clear();
      await page.getByRole('button', { name: 'Explore an idea' }).click();
      await expect(composer).toHaveValue('Help me explore an idea: ');
      await composer.fill('Help me explore better ways to plan the week.');
      await composer.press('Shift+Enter');
      await composer.pressSequentially('Include a simple example.', { delay: assetDir ? 22 : 0 });
      const original = (await composer.elementHandle())!;
      const before = (await composer.boundingBox())!.y;
      await composer.press('Enter');
      await expect(
        page.getByText('Include a simple example.', { exact: false }).first()
      ).toBeVisible();
      await expect(greeting).toHaveCount(0);
      await expect(page.getByRole('heading', { name: 'Fixture response' })).toBeVisible();
      expect(await original.evaluate(el => el === document.activeElement && el.isConnected)).toBe(
        true
      );
      await expect(composer).toHaveValue('');
      await expect.poll(async () => (await composer.boundingBox())!.y).toBeGreaterThan(before + 40);
      expect(await page.evaluate(() => scrollY)).toBe(0);
      const frames = await page.evaluate(async () => {
        while (!performance.getEntriesByName('launch-frame-samples').length)
          await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
        return (performance.getEntriesByName('launch-frame-samples')[0] as PerformanceMark)
          .detail as number[];
      });
      const sorted = frames.toSorted((a, b) => a - b);
      console.log(
        `Launch ${variant}: ${frames.length} rAF samples, p95 ${sorted[Math.floor(sorted.length * 0.95)].toFixed(1)} ms, max ${Math.max(...frames).toFixed(1)} ms, >50 ms ${frames.filter(ms => ms > 50).length}`
      );
      if (assetDir) await page.screenshot({ path: join(assetDir, `${variant}-conversation.png`) });
    } finally {
      if (assetDir) await page.screencast.stop();
    }
  });
}

test('all routes retain a fixed shell and accessible scrolling at desktop, laptop, mobile and 200% zoom', async ({
  page,
}) => {
  test.setTimeout(120_000);
  await signIn(page);
  const routes = [
    ['ai-workspace', 'AI Workspace'],
    ['tasks', 'Tasks'],
    ['approvals', 'Approvals'],
    ['trading-lab', 'Trading Lab'],
    ['device-hub', 'Device Hub'],
    ['automations', 'Automations'],
    ['memory-center', 'Memory Center'],
    ['developer-studio', 'Developer Studio'],
    ['settings', 'Settings'],
  ] as const;
  for (const [width, height] of [
    [1440, 900],
    [1280, 800],
    [1024, 768],
    [768, 1024],
    [390, 844],
    [720, 450], // 1440×900 at 200% zoom exposes a 720×450 CSS-pixel viewport.
  ]) {
    await page.setViewportSize({ width, height });
    if (width === 720) {
      const chromium = await page.context().newCDPSession(page);
      await chromium.send('Emulation.setDeviceMetricsOverride', {
        width,
        height,
        deviceScaleFactor: 2,
        mobile: false,
      });
    }
    for (const [slug, heading] of routes) {
      await page.goto(`/#/${slug}`);
      if (slug === 'ai-workspace') await expect(page.getByTestId('chat-title')).toBeVisible();
      else await expect(page.locator('#main-content h1:visible')).toHaveText(heading);
      if (width >= 1024) {
        await expect
          .poll(() =>
            page.evaluate(() => {
              const highlight = document
                .querySelector<HTMLElement>('[data-testid="navigation-highlight"]')
                ?.getBoundingClientRect();
              const active = document
                .querySelector<HTMLElement>('aside [aria-current="page"]')
                ?.getBoundingClientRect();
              return highlight && active
                ? Math.abs(highlight.y - active.y)
                : Number.POSITIVE_INFINITY;
            })
          )
          .toBeLessThan(1);
      }
      const metrics = await page.evaluate(() => {
        const main = document.querySelector<HTMLElement>('#main-content')!;
        const sidebar = document.querySelector<HTMLElement>('aside')!;
        const header = document.querySelector<HTMLElement>('header')!;
        const shell = document.querySelector<HTMLElement>('.th-workspace')!;
        return {
          viewport: { width: innerWidth, height: innerHeight },
          document: {
            width: document.documentElement.scrollWidth,
            height: document.documentElement.scrollHeight,
          },
          shell: shell.getBoundingClientRect().height,
          sidebar: sidebar.getBoundingClientRect().width,
          headerTop: header.getBoundingClientRect().top,
          mainWidth: main.clientWidth,
          mainScrollWidth: main.scrollWidth,
          mainHeight: main.clientHeight,
          mainScrollHeight: main.scrollHeight,
          mainOverflow: getComputedStyle(main).overflowY,
          navHighlight: document
            .querySelector<HTMLElement>('[data-testid="navigation-highlight"]')
            ?.getBoundingClientRect()
            .toJSON(),
          activeNav: document
            .querySelector<HTMLElement>('aside [aria-current="page"]')
            ?.getBoundingClientRect()
            .toJSON(),
        };
      });
      expect(metrics.viewport).toEqual({ width, height });
      expect(metrics.document.width, `${width}px ${slug} document width`).toBeLessThanOrEqual(
        width + 1
      );
      expect(metrics.document.height, `${height}px ${slug} document height`).toBeLessThanOrEqual(
        height + 1
      );
      expect(metrics.shell).toBe(height);
      expect(metrics.sidebar).toBe(width >= 1024 ? 252 : 0);
      if (width >= 1024) {
        expect(metrics.navHighlight).toBeDefined();
        expect(metrics.activeNav).toBeDefined();
        expect(metrics.navHighlight!.y).toBeCloseTo(metrics.activeNav!.y, 0);
        expect(metrics.navHighlight!.height).toBeCloseTo(metrics.activeNav!.height, 0);
      }
      expect(metrics.mainOverflow).toBe(slug === 'ai-workspace' ? 'hidden' : 'auto');
      expect(metrics.mainScrollWidth, `${width}px ${slug} main width`).toBeLessThanOrEqual(
        metrics.mainWidth + 1
      );
      await page.locator('#main-content').evaluate(el => {
        el.scrollTop = el.scrollHeight;
      });
      expect(await page.locator('header').evaluate(el => el.getBoundingClientRect().top)).toBe(
        metrics.headerTop
      );
      if (slug === 'settings' && metrics.mainScrollHeight > metrics.mainHeight) {
        await page.getByRole('button', { name: 'Sign out' }).scrollIntoViewIfNeeded();
        await expect(page.getByRole('button', { name: 'Sign out' })).toBeInViewport();
      }
    }
    if (width === 1280) {
      await page.getByRole('link', { name: 'Tasks' }).click();
      await expect(page.locator('#main-content')).toHaveJSProperty('scrollTop', 0);
    }
  }
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await expect(page.getByRole('dialog', { name: 'Workspace navigation' })).toBeVisible();
  await expect(page.getByRole('dialog').getByRole('link', { name: 'AI Workspace' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: 'Open navigation' })).toBeFocused();
  expect(
    (await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze())
      .violations
  ).toEqual([]);
});

test('long conversation history scrolls inside the chat without moving the application header', async ({
  page,
}) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 1280, height: 800 });
  await signIn(page, '/#/ai-workspace');
  await createConversation(page, 'Long history layout verification');
  for (let index = 0; index < 5; index++) {
    await send(page, `Layout history item ${index}: ${'a detailed prompt '.repeat(16)}`);
    await expect(page.getByRole('heading', { name: 'Fixture response' })).toHaveCount(index + 1);
  }
  const history = page.getByRole('region', { name: 'Conversation messages' });
  expect(
    await history.evaluate(el => el.scrollHeight > el.clientHeight),
    'message history must have its own scroll region'
  ).toBe(true);
  await history.evaluate(el => {
    el.scrollTop = 0;
  });
  const headerTop = await page.locator('header').evaluate(el => el.getBoundingClientRect().top);
  await history.evaluate(el => {
    el.scrollTop = el.scrollHeight;
  });
  expect(await page.locator('header').evaluate(el => el.getBoundingClientRect().top)).toBe(
    headerTop
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('textbox', { name: 'Message Jarvis' })).toBeVisible();
  await history.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBe(844);
});

test('captures the approved desktop and mobile design and checks accessibility', async ({
  page,
}, info) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await expect(page.getByLabel('Password')).toBeVisible();
  expect(
    (await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze())
      .violations
  ).toEqual([]);
  await page.screenshot({ path: info.outputPath('sign-in.png') });
  await signIn(page);
  await expect(page.getByTestId('chat-title')).toBeVisible();
  const initialNavigation = await page.evaluate(() => {
    const navigation = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming;
    return {
      domContentLoadedMs: Math.round(navigation.domContentLoadedEventEnd),
      lazyChunksOnLanding: performance
        .getEntriesByType('resource')
        .filter(entry => /\/(?:MemoryCenter|TradingLab)-[^/]+\.js$/.test(entry.name)).length,
    };
  });
  expect(initialNavigation.lazyChunksOnLanding).toBe(0);
  await info.attach('workspace-navigation.json', {
    contentType: 'application/json',
    body: JSON.stringify(initialNavigation),
  });
  console.log(
    `Local Chromium navigation: DOMContentLoaded ${initialNavigation.domContentLoadedMs} ms; unrelated chunks on AI Workspace: ${initialNavigation.lazyChunksOnLanding}`
  );
  expect(
    (await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze())
      .violations
  ).toEqual([]);
  await page.screenshot({ path: info.outputPath('desktop-workspace.png') });
  await createConversation(page, 'Web Alpha verification');
  await send(page, 'Show a test response with a code block.');
  await expect(page.getByRole('heading', { name: 'Fixture response' })).toBeVisible();
  expect(
    (await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze())
      .violations
  ).toEqual([]);
  await page.screenshot({ path: info.outputPath('desktop-conversation.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId('chat-title')).toHaveText('Web Alpha verification');
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await expect(
    page.getByRole('dialog').getByRole('link', { name: 'Web Alpha verification' })
  ).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await page.screenshot({ path: info.outputPath('mobile-conversation.png'), fullPage: true });
});

test.describe('Thozha 4 product walkthrough', () => {
  test('records all primary sections on desktop and mobile without invented data', async ({
    page,
  }, info) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    await signIn(page);
    await expect(page.getByTestId('chat-title')).toBeVisible();
    await page.screenshot({ path: info.outputPath('desktop-empty-workspace.png') });
    await createConversation(page, 'Product walkthrough');
    await expect(page.getByRole('heading', { name: "What's on your mind?" })).toBeVisible();
    await page.screenshot({ path: info.outputPath('desktop-empty-conversation.png') });
    await send(page, 'Show a test response with a code block.');
    await expect(page.getByRole('heading', { name: 'Fixture response' })).toBeVisible();
    const destinations = [
      ['ai-workspace', 'AI Workspace'],
      ['tasks', 'Tasks'],
      ['approvals', 'Approvals'],
      ['automations', 'Automations'],
      ['memory-center', 'Memory Center'],
      ['device-hub', 'Device Hub'],
      ['trading-lab', 'Trading Lab'],
      ['developer-studio', 'Developer Studio'],
      ['settings', 'Settings'],
    ] as const;
    const intervals: number[] = [];
    const cdp = await page.context().newCDPSession(page);
    let captures: Buffer[] = [];
    cdp.on('Page.screencastFrame', frame => {
      captures.push(Buffer.from(frame.data, 'base64'));
      void cdp.send('Page.screencastFrameAck', { sessionId: frame.sessionId });
    });
    await page.evaluate(() => {
      (window as Window & { startFrames?: () => void }).startFrames = () => {
        let last = 0;
        const samples: number[] = [];
        (window as Window & { frameSamples?: number[] }).frameSamples = samples;
        function tick(now: number) {
          if (last) samples.push(now - last);
          last = now;
          requestAnimationFrame(tick);
        }
        requestAnimationFrame(tick);
      };
    });
    await page.evaluate(() => (window as Window & { startFrames?: () => void }).startFrames?.());
    try {
      for (const [width, height, label] of [
        [1440, 900, 'desktop'],
        [390, 844, 'mobile'],
      ] as const) {
        await page.setViewportSize({ width, height });
        await page.waitForTimeout(150);
        captures = [];
        await cdp.send('Page.startScreencast', {
          format: 'jpeg',
          quality: 76,
          maxWidth: label === 'desktop' ? 1280 : 390,
          maxHeight: label === 'desktop' ? 800 : 844,
          everyNthFrame: 1,
        });
        for (const [slug, heading] of destinations) {
          await page.goto(`/#/${slug}`);
          if (slug === 'ai-workspace') await expect(page.getByTestId('chat-title')).toBeVisible();
          else await expect(page.locator('#main-content h1:visible')).toHaveText(heading);
          if (slug === 'tasks') {
            await page.getByLabel('What should Thozha do?').fill('Check my Mac disk space');
            await page.getByRole('button', { name: 'Review & submit' }).click();
          }
          if (slug === 'trading-lab') {
            await page.getByRole('button', { name: 'Load synthetic demo' }).click();
            await expect(page.getByText('SYNTHETIC DEMO', { exact: true })).toBeVisible();
            await expect(page.locator('.th-market-chart canvas').first()).toBeVisible();
          }
          await page.waitForTimeout(320);
          expect(
            await page.evaluate(() => document.documentElement.scrollWidth)
          ).toBeLessThanOrEqual(width + 1);
          await page.screenshot({ path: info.outputPath(`${label}-${slug}.png`) });
        }
        await cdp.send('Page.stopScreencast');
        expect(captures.length).toBeGreaterThan(20);
        const directory = info.outputPath(`${label}-capture-frames`);
        await mkdir(directory, { recursive: true });
        await Promise.all(
          captures.map((image, index) =>
            writeFile(join(directory, `frame-${String(index).padStart(5, '0')}.jpg`), image)
          )
        );
        const encoded = spawnSync(
          'ffmpeg',
          [
            '-v',
            'error',
            '-y',
            '-framerate',
            '12',
            '-i',
            join(directory, 'frame-%05d.jpg'),
            '-c:v',
            'libvpx-vp9',
            '-b:v',
            '0',
            '-crf',
            '35',
            info.outputPath(`${label}-walkthrough.webm`),
          ],
          { encoding: 'utf8' }
        );
        if (encoded.status !== 0)
          throw new Error(`Could not encode the ${label} recording: ${encoded.stderr}`);
        await rm(directory, { recursive: true });
      }
    } finally {
      await cdp.detach();
    }
    intervals.push(
      ...(await page.evaluate(
        () => (window as Window & { frameSamples?: number[] }).frameSamples ?? []
      ))
    );
    const sorted = intervals.filter(value => value > 0).sort((a, b) => a - b);
    const metrics = {
      frameCount: sorted.length,
      p95FrameIntervalMs: sorted[Math.floor(sorted.length * 0.95)] ?? null,
      intervalsOver25ms: sorted.filter(value => value > 25).length,
    };
    expect(metrics.frameCount).toBeGreaterThan(30);
    await info.attach('interaction-frames.json', {
      body: JSON.stringify(metrics),
      contentType: 'application/json',
    });
    await writeFile(info.outputPath('interaction-frames.json'), JSON.stringify(metrics, null, 2));
    console.log(
      `Recorded interaction frame intervals: p95 ${metrics.p95FrameIntervalMs?.toFixed(2)} ms, ${metrics.intervalsOver25ms} over 25 ms in ${metrics.frameCount} frames (recording on).`
    );
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.evaluate(() => (window as Window & { startFrames?: () => void }).startFrames?.());
    for (let index = 0; index < 8; index++) {
      await page.goto(index % 2 ? '/#/ai-workspace' : '/#/tasks');
      await page.waitForTimeout(300);
    }
    const unrecorded = await page.evaluate(
      () => (window as Window & { frameSamples?: number[] }).frameSamples ?? []
    );
    const stable = unrecorded.filter(value => value > 0).sort((a, b) => a - b);
    const runtime = {
      frameCount: stable.length,
      p95FrameIntervalMs: stable[Math.floor(stable.length * 0.95)] ?? null,
      intervalsOver25ms: stable.filter(value => value > 25).length,
    };
    expect(runtime.frameCount).toBeGreaterThan(30);
    await writeFile(info.outputPath('unrecorded-frames.json'), JSON.stringify(runtime, null, 2));
    console.log(
      `Unrecorded navigation frame intervals: p95 ${runtime.p95FrameIntervalMs?.toFixed(2)} ms, ${runtime.intervalsOver25ms} over 25 ms in ${runtime.frameCount} frames.`
    );
  });
});
