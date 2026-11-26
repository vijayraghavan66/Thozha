import { useState } from 'react';
import type { WorkspaceApi } from '../api/client';
import { inventoryUnavailable, presenceLabel } from '../api/devicePlatform';
import { Head, Panel, Chip, Loading, Modal, Notice, Row } from '../components/ui';
import { formatDate, muted, outline, solid } from '../components/styles';
import { controlError } from './controlState';
import { useDevicePlatform } from './devicePlatformState';
import { CapabilityList } from './DevicePlatformDetails';

const installation = {
  macos: {
    name: 'macOS',
    state: 'Existing Beta 1 host',
    description:
      'Reuse the existing OpenJarvis supervisor and execution runtime. Do not install a second runtime or reset its identity.',
  },
  windows: {
    name: 'Windows',
    state: 'Future execution agent',
    description:
      'No Windows execution agent or verified installer is available in the reviewed Beta 1 source.',
  },
  linux: {
    name: 'Linux',
    state: 'Future execution agent',
    description:
      'No Linux execution agent or verified installer is available in the reviewed Beta 1 source.',
  },
} as const;
type InstallPlatform = keyof typeof installation;

export default function AgentHub({ api, connected }: { api: WorkspaceApi; connected: boolean }) {
  const { inventory, online, now } = useDevicePlatform(api, connected);
  const [install, setInstall] = useState<InstallPlatform>();
  const [selected, select] = useState('');
  const data = inventory.data;
  const agents = data?.agents ?? [];
  const active = agents.find(item => item.agent_id === selected) ?? agents[0];
  return (
    <>
      <Head
        label="AUTHORIZED EXECUTION"
        title="Agents"
        desc="One Web workspace. Execution stays with your authorized host and connected agents."
        action={
          <button className={solid} type="button" onClick={() => setInstall('macos')}>
            Install Agent
          </button>
        }
      />
      {!online && (
        <Notice>
          Browser offline or session unavailable. Agent connectivity cannot be verified. Reconnect
          to refresh.
        </Notice>
      )}
      <Panel cls="mb-5 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold">Installed agents</h2>
            <p className={`mt-2 ${muted}`}>
              Only host-reported inventory appears here. A paired phone is not automatically an
              execution agent.
            </p>
          </div>
          <button
            type="button"
            className={outline}
            disabled={!online || inventory.isFetching}
            onClick={() => void inventory.refetch()}
          >
            Refresh agents
          </button>
        </div>
        {inventory.isPending && online && <Loading label="Checking agent inventory…" />}
        {inventory.isError && (
          <div className="mt-4">
            <Notice
              error={!inventoryUnavailable(inventory.error)}
              retry={online ? () => void inventory.refetch() : undefined}
            >
              {inventoryUnavailable(inventory.error)
                ? 'Agent inventory is not available on this host version. This does not mean OpenJarvis is uninstalled or offline. No agents or download links have been invented.'
                : controlError(inventory.error, 'read installed agents')}
            </Notice>
          </div>
        )}
        {inventory.isSuccess && !agents.length && (
          <p className={`mt-5 ${muted}`}>
            The host returned no agents for this Web owner. Existing runtime installation and owner
            enrollment are separate.
          </p>
        )}
      </Panel>
      {data && active && (
        <div className="th-v4-device-layout mb-5">
          <div className="th-v4-device-list" aria-label="Installed execution agents">
            {agents.map(item => (
              <button
                key={item.agent_id}
                type="button"
                className={`${outline} min-w-0 break-words p-4 text-left`}
                aria-pressed={active.agent_id === item.agent_id}
                onClick={() => select(item.agent_id)}
              >
                <b className="block">{item.name}</b>
                <span className={`mt-2 block ${muted}`}>
                  {item.platform} ·{' '}
                  {presenceLabel(item.presence, data, online && !inventory.isError, now)}
                </span>
              </button>
            ))}
          </div>
          <Panel cls="min-w-0 p-5">
            <h2 className="break-words text-base font-semibold">{active.name}</h2>
            <Row
              main="Runtime"
              sub={`${active.runtime} · ${active.version ?? 'Version not reported'}`}
              right={<Chip>{active.lifecycle}</Chip>}
            />
            <Row
              main="Connection"
              sub={presenceLabel(active.presence, data, online && !inventory.isError, now)}
            />
            <Row
              main="Last seen"
              sub={
                active.presence.last_seen_at
                  ? formatDate(active.presence.last_seen_at)
                  : 'Not reported'
              }
            />
            <Row main="Agent identity" sub={active.agent_id} />
            <Row
              main="Linked device"
              sub={active.device_id ?? 'No linked companion identity reported'}
            />
            <h3 className="mt-4 text-sm font-semibold">Capabilities & permissions</h3>
            <CapabilityList items={active.capabilities} />
            <p className={muted}>
              Lifecycle and permission changes are not exposed by the current backend. Agent removal
              must not be confused with companion revocation.
            </p>
            <a href="#/tasks" className="mt-4 inline-block text-xs underline">
              Open task timeline
            </a>
          </Panel>
        </div>
      )}
      <Panel cls="p-5">
        <h2 className="text-sm font-semibold">Install & connect</h2>
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          {Object.entries(installation).map(([key, item]) => (
            <button
              key={key}
              type="button"
              className={`${outline} min-w-0 whitespace-normal p-4 text-left`}
              onClick={() => setInstall(key as InstallPlatform)}
            >
              <b className="block">{item.name}</b>
              <span className={`mt-2 block ${muted}`}>{item.state}</span>
            </button>
          ))}
        </div>
        <p className={`mt-5 ${muted}`}>
          Android uses the existing Kotlin companion and authenticated QR pairing. iOS is planned:
          foreground/user-mediated actions only, with no promise of unrestricted background
          execution or cross-app control.
        </p>
        <a href="#/device-hub" className="mt-3 inline-block text-xs underline">
          Pair or recover a companion
        </a>
      </Panel>
      {install && (
        <Modal
          title={`Install Agent · ${installation[install].name}`}
          close={() => setInstall(undefined)}
        >
          <Chip>{installation[install].state}</Chip>
          <p className={`mt-4 ${muted}`}>{installation[install].description}</p>
          <Notice>
            No verified downloadable artifact is published in the reviewed source. Download and
            enrollment are unavailable here.
          </Notice>
          <h3 className="mt-5 text-sm font-semibold">Compatibility & enrollment requirements</h3>
          <ul className={`mt-3 list-disc space-y-2 pl-4 ${muted}`}>
            <li>
              Versioned agent protocol, host identity verification and explicit owner enrollment are
              required before task routing.
            </li>
            <li>
              Capabilities must be reported by the runtime and authorized separately. Installation
              never grants blanket execution permission.
            </li>
            <li>
              The existing supervisor remains the final task and approval authority. No terminal
              commands, enrollment secrets or new identity store are supplied by this screen.
            </li>
            <li>
              For an existing Mac, check the saved host connection and Web-owner mapping. Do not
              restart shared services or replace pairing certificates.
            </li>
          </ul>
          <button
            type="button"
            data-dialog-focus
            className={`${outline} mt-5`}
            onClick={() => setInstall(undefined)}
          >
            Done
          </button>
        </Modal>
      )}
    </>
  );
}
