import type { WorkspaceApi } from '../api/client';
import { inventoryUnavailable, presenceLabel, type Capability } from '../api/devicePlatform';
import { Chip, Notice, Row } from '../components/ui';
import { formatDate, muted } from '../components/styles';
import { controlError } from './controlState';
import { useDevicePlatform } from './devicePlatformState';

export function CapabilityList({ items }: { items: Capability[] }) {
  return items.length ? (
    <ul aria-label="Reported capabilities" className="divide-y divide-white/10">
      {items.map((item, index) => (
        <li
          key={`${item.id}-${index}`}
          className="flex flex-wrap items-center justify-between gap-3 py-3 text-xs"
        >
          <span className="min-w-0 break-words">
            {item.label}
            <span className={`mt-1 block break-all ${muted}`}>{item.id}</span>
          </span>
          <Chip>{!item.supported ? 'Unsupported' : item.authorization.replace(/_/g, ' ')}</Chip>
        </li>
      ))}
    </ul>
  ) : (
    <p className={`py-3 ${muted}`}>
      No capabilities reported. This does not grant permission to execute actions.
    </p>
  );
}

export default function DevicePlatformDetails({
  api,
  connected,
  deviceId,
}: {
  api: WorkspaceApi;
  connected: boolean;
  deviceId: string;
}) {
  const { inventory, online, now } = useDevicePlatform(api, connected);
  const data = inventory.data;
  const facts = data?.devices.find(item => item.device_id === deviceId);
  return (
    <div className="mt-4" aria-label="Device connectivity and permissions">
      {!online && (
        <Notice>
          Browser offline or session unavailable. Saved details are not a live connection check.
        </Notice>
      )}
      {inventory.isPending && online && (
        <p role="status" className={muted}>
          Checking device reporting support…
        </p>
      )}
      {inventory.isError && (
        <Notice
          error={!inventoryUnavailable(inventory.error)}
          retry={online ? () => void inventory.refetch() : undefined}
        >
          {inventoryUnavailable(inventory.error)
            ? 'This host does not yet expose device presence, versions or capability permissions. The active pairing below is real; connectivity is unknown.'
            : controlError(inventory.error, 'read device connectivity')}
        </Notice>
      )}
      {facts && data ? (
        <>
          <Row
            main="Connection"
            sub={presenceLabel(facts.presence, data, online && !inventory.isError, now)}
          />
          <Row
            main="Last seen by host"
            sub={
              facts.presence.last_seen_at ? formatDate(facts.presence.last_seen_at) : 'Not reported'
            }
          />
          <Row
            main="Platform / OS"
            sub={`${facts.platform} · ${facts.os_version ?? 'Version not reported'}`}
          />
          <Row main="Companion version" sub={facts.app_version ?? 'Not reported'} />
          <h3 className="mt-4 text-sm font-semibold">Capabilities & authorization</h3>
          <CapabilityList items={facts.capabilities} />
          <p className={muted}>
            Reported permissions are informational. The supervisor rechecks authorization and
            exact-operation approval for every task.
          </p>
        </>
      ) : (
        <Row main="Connection, versions & permissions" sub="Unknown · no device report available" />
      )}
      <details className="mt-5 text-xs leading-6">
        <summary className="cursor-pointer font-medium">Recovery & reconnect guidance</summary>
        <ol className={`mt-2 list-decimal space-y-2 pl-5 ${muted}`}>
          <li>
            Open the existing companion and use its saved paired host. Do not clear app data, reset
            keys or create another identity.
          </li>
          <li>
            Check the owner-approved LAN or existing USB connection. A browser refresh cannot repair
            host transport.
          </li>
          <li>
            If the phone is paired but absent here, ask the host owner to verify its Web-owner
            mapping. Pairing alone does not grant Web task access.
          </li>
          <li>
            Only revoke and pair again if access must intentionally be replaced. Revocation
            invalidates the companion credential.
          </li>
        </ol>
      </details>
      <a href="#/tasks" className="mt-4 inline-block text-xs underline underline-offset-4">
        Open existing task timeline
      </a>
      <span aria-hidden="true"> · </span>
      <a href="#/approvals" className="text-xs underline underline-offset-4">
        Review approvals
      </a>
    </div>
  );
}
