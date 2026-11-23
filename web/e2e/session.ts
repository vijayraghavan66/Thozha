import { expect, type Page } from '@playwright/test';

export async function signIn(page: Page, path = '/') {
  const password = process.env.THOZHA_E2E_PASSWORD;
  if (!password) throw new Error('Playwright fixture password is missing');
  await page.goto(path);
  await page.getByLabel('Workspace ID').fill('web-e2e-owner');
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByText('WEB ALPHA', { exact: true })).toBeVisible();
}
