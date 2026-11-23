import { expect, test, type Locator, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { mkdir, writeFile, rm } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { signIn } from './session';

declare global {
  interface Window {
    waterProbe: { contexts: number; draws: number; deletes: number; strength: number };
    restoreWaterContext?: () => void;
  }
}

async function observeWater(page: Page, blocked = false) {
  await page.addInitScript(blocked => {
    const probe = { contexts: 0, draws: 0, deletes: 0, strength: 0 };
    window.waterProbe = probe;
    const context = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, type, attributes) {
      if (type === 'webgl2') {
        probe.contexts++;
        if (blocked) return null;
      }
      return Reflect.apply(context, this, [type, attributes]);
    } as typeof context;
    if (typeof WebGL2RenderingContext === 'undefined') return;
    const gl = WebGL2RenderingContext.prototype;
    const { drawArrays, deleteProgram, uniform4fv } = gl;
    gl.drawArrays = function (...args) {
      probe.draws++;
      drawArrays.apply(this, args);
    };
    gl.deleteProgram = function (program) {
      probe.deletes++;
      deleteProgram.call(this, program);
    };
    gl.uniform4fv = function (...args) {
      const values = args[1];
      for (let i = 3; i < values.length; i += 4) {
        probe.strength = Math.max(probe.strength, values[i]);
      }
      uniform4fv.apply(this, args);
    };
  }, blocked);
}

async function expectNoWaterDraws(page: Page) {
  const draws = await page.evaluate(() => window.waterProbe.draws);
  await page.waitForTimeout(180);
  expect(await page.evaluate(() => window.waterProbe.draws)).toBe(draws);
}

function waterBackingSize(canvas: Locator) {
  return canvas.evaluate(el => {
    if (!(el instanceof HTMLCanvasElement)) throw new Error('Water canvas is missing');
    return [el.width, el.height] as const;
  });
}

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
async function send(page: Page, content: string) {
  await page.getByRole('textbox', { name: 'Message Jarvis' }).fill(content);
  await page.getByRole('button', { name: /Send ↑/ }).click();
}

test('mobile slash commands stay local and refuse tasks without verified readiness', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  await page.locator('main').getByRole('button', { name: '＋ New conversation' }).click();
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
  await expect(page.getByLabel('Objective')).toHaveValue('Read host status');
  await expect(page.getByText(/No active device mapping was verified/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Submit task', exact: true })).toBeDisabled();
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
  await page.getByRole('button', { name: 'Copy code' }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    "const workspace = 'Thozha Alpha';"
  );
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
  await page.getByRole('checkbox', { name: 'Show archived' }).check();
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
  await expect(page.getByRole('heading', { name: 'AI Workspace' })).toBeVisible();
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('link', { name: 'Trading Lab' }).click();
  await expect(page.getByRole('heading', { name: 'Trading Lab' })).toBeVisible();
  await page.getByRole('button', { name: 'Paper order', exact: true }).click();
  await page.getByRole('button', { name: 'Simulate buy' }).click();
  await expect(page.getByText('Buy 5 NIFTYBEES')).toBeVisible();
  await page.getByRole('button', { name: 'Backtests' }).click();
  await expect(page.getByRole('button', { name: 'Run backtest' })).toBeDisabled();
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
  await expect(page.getByRole('button', { name: 'Run project checks' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'New supervisor task' })).toBeDisabled();
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
  await expect(page.getByRole('heading', { name: 'AI Workspace' })).toBeVisible();
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

test('the compact Water Lab material draws ripples, resizes and releases GPU work on navigation', async ({
  page,
}) => {
  await observeWater(page);
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await signIn(page);
  await createConversation(page, 'Water material verification');
  const water = page.locator('main .th-liquid-field');
  const canvas = water.locator('canvas');
  const canRender = await canvas.evaluate(el =>
    Boolean(
      el.ownerDocument
        .createElement('canvas')
        .getContext('webgl2', { failIfMajorPerformanceCaveat: true })
    )
  );
  test.skip(
    !canRender,
    'Chromium has no accelerated WebGL 2 context; the separate fallback test still runs.'
  );
  await expect(water).toHaveAttribute('data-water', 'webgl');
  await expect.poll(() => page.evaluate(() => window.waterProbe.draws)).toBeGreaterThan(2);
  const size = await waterBackingSize(canvas);
  expect(size[0] * size[1]).toBeLessThanOrEqual(260_000);
  if ((await water.getAttribute('data-quality')) === 'balanced') {
    const box = (await water.boundingBox())!;
    await page.mouse.click(box.x + box.width * 0.8, box.y + box.height * 0.74);
    await expect.poll(() => page.evaluate(() => window.waterProbe.strength)).toBeGreaterThan(0.012);
  }
  const session = await page.context().newCDPSession(page);
  await session.send('Emulation.setDeviceMetricsOverride', {
    width: 390,
    height: 844,
    deviceScaleFactor: 2,
    mobile: false,
  });
  await expect(water).toHaveAttribute('data-quality', 'economy');
  await expect
    .poll(async () => {
      const [width, height] = await waterBackingSize(canvas);
      return width * height;
    })
    .toBeLessThanOrEqual(96_000);
  expect((await waterBackingSize(canvas))[0]).toBeLessThanOrEqual(size[0]);
  await page.goto('/#/tasks');
  await expect(page.getByRole('heading', { name: 'Tasks', exact: true })).toBeVisible();
  await expect(water).toBeHidden();
  expect(await page.evaluate(() => window.waterProbe.deletes)).toBeGreaterThan(0);
  await expectNoWaterDraws(page);
});

test('Water Lab suspension stops actual draws, and a restored WebGL context resumes them', async ({
  page,
}) => {
  await page.setViewportSize({ width: 900, height: 520 });
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await observeWater(page);
  await signIn(page);
  await createConversation(page, 'Water lifecycle verification');
  const water = page.locator('main .th-liquid-field');
  const canvas = water.locator('canvas');
  const canRender = await canvas.evaluate(el =>
    Boolean(
      el.ownerDocument
        .createElement('canvas')
        .getContext('webgl2', { failIfMajorPerformanceCaveat: true })
    )
  );
  test.skip(!canRender, 'Chromium has no accelerated WebGL 2 context.');
  await expect(water).toHaveAttribute('data-water', 'webgl');
  const host = page.locator('.th-v4-empty-material');
  await host.evaluate(el => {
    (el as HTMLElement).style.transform = 'translate(-50%, 1200px)';
  });
  await expect(water).toHaveAttribute('data-motion', 'paused');
  await expectNoWaterDraws(page);
  await host.evaluate(el => {
    (el as HTMLElement).style.transform = '';
  });
  await expect(water).toHaveAttribute('data-motion', 'running');
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await expect(water).toHaveAttribute('data-motion', 'paused');
  await expectNoWaterDraws(page);
  await page.evaluate(() => {
    Reflect.deleteProperty(document, 'hidden');
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await expect(water).toHaveAttribute('data-motion', 'running');
  const lost = await canvas.evaluate(el => {
    if (!(el instanceof HTMLCanvasElement)) throw new Error('Water canvas is missing');
    const extension = el.getContext('webgl2')?.getExtension('WEBGL_lose_context');
    if (!extension) return false;
    window.restoreWaterContext = () => extension.restoreContext();
    extension.loseContext();
    return true;
  });
  expect(lost).toBe(true);
  await expect(water).toHaveAttribute('data-water', 'poster');
  await expect(water).toHaveAttribute('data-motion', 'paused');
  await expectNoWaterDraws(page);
  const previous = await page.evaluate(() => window.waterProbe.draws);
  await page.evaluate(() => window.restoreWaterContext?.());
  await expect(water).toHaveAttribute('data-water', 'webgl');
  await expect.poll(() => page.evaluate(() => window.waterProbe.draws)).toBeGreaterThan(previous);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await expect(water).toHaveAttribute('data-water', 'poster');
  await expect(water).toHaveAttribute('data-motion', 'paused');
  await expect.poll(() => page.evaluate(() => window.waterProbe.deletes)).toBeGreaterThan(0);
  await expectNoWaterDraws(page);
});

test('the shader stays off for reduced motion and the renderer has a still fallback', async ({
  page,
}) => {
  await observeWater(page, true);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await signIn(page);
  await createConversation(page, 'Water fallback verification');
  const water = page.locator('main .th-liquid-field');
  await expect(water).toHaveAttribute('data-water', 'poster');
  await expect(water).toHaveAttribute('data-motion', 'paused');
  expect(await page.evaluate(() => window.waterProbe.contexts)).toBe(0);
  const still = await water.evaluate(el => getComputedStyle(el).backgroundImage);
  expect(still).toMatch(/water-still-.*\.webp/);
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await expect.poll(() => page.evaluate(() => window.waterProbe.contexts)).toBe(1);
  await expect(water).toHaveAttribute('data-water', 'poster');
  await expect(water).toHaveAttribute('data-motion', 'paused');
  await expect(water.locator('canvas')).toBeHidden();
  await expectNoWaterDraws(page);

  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'Reduce motion: off' }).click();
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.getByRole('link', { name: 'AI Workspace' }).click();
  await page.reload();
  await expect(water).toHaveAttribute('data-water', 'poster');
  await expect(water).toHaveAttribute('data-motion', 'paused');
  expect(await page.evaluate(() => window.waterProbe.contexts)).toBe(0);
});

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
      await expect(page.locator('#main-content h1:visible')).toHaveText(heading);
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
  await expect(page.getByRole('heading', { name: 'AI Workspace' })).toBeVisible();
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
  await expect(page.getByLabel('Current conversation')).toBeVisible();
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
    await expect(page.getByRole('heading', { name: 'AI Workspace' })).toBeVisible();
    await page.screenshot({ path: info.outputPath('desktop-empty-workspace.png') });
    await createConversation(page, 'Product walkthrough');
    await expect(page.getByRole('heading', { name: 'Where should we begin?' })).toBeVisible();
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
          await expect(page.locator('#main-content h1:visible')).toHaveText(heading);
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
