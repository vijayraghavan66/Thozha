import { expiringInvitation } from './device-expiry.fixture';
import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { signIn } from './session';

const deviceId = 'a'.repeat(32);
const invitationId = 'b'.repeat(32);
function report() {
  const now = new Date().toISOString();
  const capabilities = [
    {
      id: 'files.read',
      label: 'Read selected files',
      supported: true,
      authorization: 'approval_required',
    },
  ];
  return {
    contract_version: 1,
    observed_at: now,
    stale_after_seconds: 30,
    devices: [
      {
        device_id: deviceId,
        platform: 'android',
        os_version: '16',
        app_version: 'Beta 1 fixture',
        presence: { state: 'online', last_seen_at: now },
        capabilities,
      },
    ],
    agents: [
      {
        agent_id: 'c'.repeat(32),
        device_id: null,
        name: 'Isolated OpenJarvis fixture',
        runtime: 'OpenJarvis',
        platform: 'macos',
        version: 'fixture-only',
        lifecycle: 'active',
        presence: { state: 'online', last_seen_at: now },
        capabilities,
      },
    ],
  };
}
async function deviceRoutes(page: Page) {
  const device = {
    device_id: deviceId,
    device_name: 'Fixture Android companion',
    created_at: '2026-11-01T00:00:00Z',
    expires_at: '2099-01-01T00:00:00Z',
    status: 'active',
  };
  await page.route(/\/v1\/web\/devices(?:\?.*)?$/, route =>
    route.fulfill({ json: { devices: [device], count: 1 } })
  );
  await page.route(`**/v1/web/devices/${deviceId}`, route => route.fulfill({ json: device }));
  await page.route(/\/v1\/web\/devices\/pairing-requests\?/, route =>
    route.fulfill({ json: { requests: [], count: 0 } })
  );
}

test('real inventory adapter, permissions, offline/reconnect, responsive layout and accessibility', async ({
  page,
}) => {
  await deviceRoutes(page);
  await page.route('**/v1/web/device-platform/inventory', route =>
    route.fulfill({ json: report() })
  );
  await signIn(page, '/#/device-hub');
  await expect(page.getByText('Beta 1 fixture', { exact: true })).toBeVisible();
  await expect(page.getByText(/^Read selected files/)).toBeVisible();
  await expect(page.getByText('Online', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Manage execution agents' }).click();
  await expect(page.getByRole('heading', { name: 'Isolated OpenJarvis fixture' })).toBeVisible();
  for (const width of [1440, 768, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.getByRole('heading', { name: 'Agents', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true
    );
    for (const colorScheme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme });
      const audit = await new AxeBuilder({ page }).include('#main-content').analyze();
      expect(audit.violations).toEqual([]);
    }
  }
  await page.context().setOffline(true);
  await expect(page.getByText('Unknown · browser offline', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Refresh agents' })).toBeDisabled();
  await page.context().setOffline(false);
  await expect(page.getByText('Online', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Install Agent', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText(/No verified downloadable artifact/)).toBeVisible();
  await expect(dialog.getByRole('link')).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(dialog).not.toBeVisible();
});

test('unsupported inventory is honest, not empty or fabricated; future installers remain unavailable', async ({
  page,
}) => {
  await page.route('**/v1/web/device-platform/inventory', route =>
    route.fulfill({ status: 404, json: { detail: 'Not mounted' } })
  );
  await signIn(page, '/#/agents');
  await expect(
    page.getByText(/Agent inventory is not available on this host version/)
  ).toBeVisible();
  await expect(page.getByText(/host returned no agents/)).toHaveCount(0);
  for (const name of ['Windows', 'Linux']) {
    await page
      .getByRole('button', { name: new RegExp(`^${name} Future execution agent$`) })
      .click();
    await expect(
      page.getByRole('dialog').getByText(/No verified downloadable artifact/)
    ).toBeVisible();
    await page.getByRole('button', { name: 'Done', exact: true }).click();
  }
});

test('inventory errors cannot show cached data as live', async ({ page }) => {
  let fail = false;
  await page.route('**/v1/web/device-platform/inventory', route =>
    fail
      ? route.fulfill({ status: 503, json: { detail: 'Fixture unavailable' } })
      : route.fulfill({ json: report() })
  );
  await signIn(page, '/#/agents');
  await expect(page.getByText('Online', { exact: true })).toBeVisible();
  fail = true;
  await page.getByRole('button', { name: 'Refresh agents' }).click();
  await expect(page.getByText('Fixture unavailable', { exact: true })).toBeVisible();
  await expect(page.getByText('Online', { exact: true })).toHaveCount(0);
});

test('isolated QR lifecycle: scan, owner approval, claim and rejection use genuine adapter contracts', async ({
  page,
}) => {
  await deviceRoutes(page);
  let status = 'INVITED';
  const expires_at = new Date(Date.now() + 120_000).toISOString();
  const fingerprint = `sha256:${'d'.repeat(64)}`;
  const payload = {
    protocol: 'openjarvis-pairing-v2',
    server_url: 'https://192.0.2.1:8443',
    gateway_address: '192.0.2.1',
    gateway_port: '8443',
    gateway_certificate_sha256: fingerprint,
    server_fingerprint: fingerprint,
    server_public_key: 'fixture-public-only',
    invitation_id: invitationId,
    challenge: 'fixture-challenge',
    expires_at,
    server_signature: 'fixture-signature',
  };
  await page.route('**/v1/web/devices/pairing-invitations', route =>
    route.fulfill({ json: { ...payload, qr_payload: payload } })
  );
  await page.route(`**/v1/web/devices/pairing-invitations/${invitationId}`, route =>
    route.fulfill({ json: { invitation_id: invitationId, status } })
  );
  await page.route(/\/v1\/web\/devices\/pairing-requests\?/, route =>
    route.fulfill({
      json: {
        requests:
          status === 'PENDING_CONFIRMATION'
            ? [
                {
                  id: invitationId,
                  status,
                  device_name: 'QR fixture phone',
                  device_fingerprint: fingerprint,
                  requested_at: new Date().toISOString(),
                  expires_at,
                },
              ]
            : [],
        count: status === 'PENDING_CONFIRMATION' ? 1 : 0,
      },
    })
  );
  await page.route(`**/v1/web/devices/pairing-requests/${invitationId}/decision`, async route => {
    expect(route.request().headers()['x-thozha-csrf']).toBeTruthy();
    expect(route.request().headers()['authorization']).toBeUndefined();
    status = route.request().postDataJSON().decision === 'approve' ? 'APPROVED' : 'REJECTED';
    await route.fulfill({ json: { invitation_id: invitationId, status } });
  });
  await signIn(page, '/#/device-hub');
  await page.getByRole('button', { name: 'Pair new device', exact: true }).click();
  await expect(
    page.getByRole('img', { name: 'Signed Wi-Fi pairing invitation QR code' })
  ).toBeVisible();
  await expect(page.getByText(/Expires in/)).toBeVisible();
  status = 'PENDING_CONFIRMATION';
  await expect(
    page.getByRole('img', { name: 'Signed Wi-Fi pairing invitation QR code' })
  ).toHaveCount(0);
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Refresh requests' }).click();
  await page.getByRole('button', { name: 'Review identity' }).click();
  await page.getByRole('button', { name: 'Approve pairing', exact: true }).click();
  await expect(
    page.getByText(/Owner approval recorded. The phone must still complete/)
  ).toBeVisible();
  status = 'TOKEN_ISSUED';
  await page.getByRole('button', { name: 'View invitation' }).click();
  await expect(page.getByText(/phone completed pairing/)).toBeVisible();
  await page.keyboard.press('Escape');
  status = 'PENDING_CONFIRMATION';
  await page.getByRole('button', { name: 'Refresh requests' }).click();
  await page.getByRole('button', { name: 'Review identity' }).click();
  await page.getByRole('button', { name: 'Reject request', exact: true }).click();
  await expect(page.getByText('Pairing request rejected.', { exact: true })).toBeVisible();
});

test('failed invitation creation never fabricates a QR', async ({ page }) => {
  await deviceRoutes(page);
  await page.route('**/v1/web/devices/pairing-invitations', route =>
    route.fulfill({ status: 409, json: { detail: 'Private gateway is not configured' } })
  );
  await signIn(page, '/#/device-hub');
  await page.getByRole('button', { name: 'Pair new device', exact: true }).click();
  await expect(
    page.getByRole('alert').filter({ hasText: 'Private connection is not set up' })
  ).toBeVisible();
  await expect(
    page.getByRole('img', { name: 'Signed Wi-Fi pairing invitation QR code' })
  ).toHaveCount(0);
});

test('QR expiry, cancellation and offline hiding do not reset an existing identity', async ({
  page,
}) => {
  await deviceRoutes(page);
  let status = 'INVITED';
  let invitations = 0;
  await page.clock.install();
  await page.route('**/v1/web/devices/pairing-invitations', route =>
    route.fulfill({
      json: expiringInvitation(
        invitationId,
        new Date(Date.now() + (++invitations === 1 ? 3000 : 60000)).toISOString()
      ),
    })
  );
  await page.route(`**/v1/web/devices/pairing-invitations/${invitationId}`, route =>
    route.fulfill({ json: { invitation_id: invitationId, status } })
  );
  await page.route(`**/v1/web/devices/pairing-invitations/${invitationId}/cancel`, route => {
    status = 'CANCELLED';
    return route.fulfill({ json: { invitation_id: invitationId, status } });
  });
  await signIn(page, '/#/device-hub');
  await page.getByRole('button', { name: 'Pair new device', exact: true }).click();
  const qr = page.getByRole('img', { name: 'Signed Wi-Fi pairing invitation QR code' });
  await expect(qr).toBeVisible();
  await page.clock.fastForward(5000);
  await expect(qr).toHaveCount(0);
  await expect(page.getByText('This QR invitation has expired.', { exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Pair new device', exact: true }).click();
  await expect(qr).toBeVisible();
  await page.context().setOffline(true);
  await expect(qr).toHaveCount(0);
  await page.context().setOffline(false);
  await expect(qr).toBeVisible();
  await page.getByRole('button', { name: 'Cancel invitation', exact: true }).click();
  await expect(qr).toHaveCount(0);
  await expect(page.getByText('This invitation was cancelled.', { exact: true })).toBeVisible();
});
