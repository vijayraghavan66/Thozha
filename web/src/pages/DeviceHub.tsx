import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { WorkspaceApi } from '../api/client';
import type { PairedDevice } from '../api/control';
import { LiquidCard } from '../components/LiquidSurface';
import { formatDate, muted, outline, solid } from '../components/styles';
import { Chip, Head, Loading, Modal, Notice, Panel, Row } from '../components/ui';
import { DeviceExamples } from './WorkspacePages';
import {
  approvalsKey,
  automationCapabilitiesKey,
  controlError,
  devicesKey,
  httpStatus,
  tasksKey,
  useDevices,
} from './controlState';

export default function DeviceHub({ api, connected }: { api: WorkspaceApi; connected: boolean }) {
  const cache = useQueryClient();
  const devices = useDevices(api, connected);
  const [selected, select] = useState('');
  const [confirm, setConfirm] = useState<PairedDevice>();
  const [message, setMessage] = useState('');
  const items = devices.isSuccess ? devices.data.devices : [];
  const deviceId = items.some(item => item.device_id === selected) ? selected : items[0]?.device_id;
  const detail = useQuery({
    queryKey: ['web-control', 'device', deviceId],
    queryFn: ({ signal }) => api.getDevice(deviceId!, signal),
    enabled: connected && Boolean(deviceId),
  });
  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeDevice(id),
    onSuccess: result => {
      setMessage(
        `${result.status === 'already_revoked' ? 'The pairing was already revoked.' : 'Device pairing revoked.'} ${result.notification_token_cleanup === 'revoked' ? 'The backend revoked its notification registration.' : 'The backend reports no notification registration to revoke.'}`
      );
      setConfirm(undefined);
      select('');
      cache.setQueryData<typeof devices.data>(
        devicesKey,
        previous =>
          previous && {
            devices: previous.devices.filter(item => item.device_id !== result.device_id),
            count: Math.max(
              0,
              previous.count -
                Number(previous.devices.some(item => item.device_id === result.device_id))
            ),
          }
      );
      cache.removeQueries({ queryKey: ['web-control', 'device', result.device_id] });
      void cache.invalidateQueries({ queryKey: devicesKey });
      void cache.invalidateQueries({ queryKey: tasksKey });
      void cache.invalidateQueries({ queryKey: approvalsKey });
      void cache.invalidateQueries({ queryKey: automationCapabilitiesKey });
    },
  });
  if ([403, 404].includes(httpStatus(devices.error) ?? 0)) {
    return (
      <>
        {message && <Notice>{message}</Notice>}
        <DeviceExamples />
        <div className="mt-3">
          <Notice retry={connected ? () => void devices.refetch() : undefined}>
            {controlError(devices.error, 'list mapped devices')}
          </Notice>
        </div>
      </>
    );
  }
  const pairing = detail.isSuccess ? detail.data : undefined;
  return (
    <>
      <Head
        label="CONSENT-FIRST ECOSYSTEM"
        title="Device Hub"
        desc="Review active companion pairings explicitly mapped to this Web owner. Pairing is not an online or reachability check."
        action={
          <button
            type="button"
            className={solid}
            disabled={!connected || devices.isFetching}
            onClick={() => void devices.refetch()}
          >
            Refresh devices
          </button>
        }
      />
      {message && (
        <div className="mb-4">
          <Notice>{message}</Notice>
        </div>
      )}
      {devices.isPending && connected && <Loading label="Loading active paired devices…" />}
      {devices.isError && (
        <Notice error retry={connected ? () => void devices.refetch() : undefined}>
          {controlError(devices.error, 'list mapped devices')}
        </Notice>
      )}
      {devices.isSuccess && items.length === 0 && (
        <Panel cls="p-6">
          <h2 className="font-semibold">No active mapped devices</h2>
          <p className={`mt-2 ${muted}`}>
            Pair and explicitly map a device outside the browser. No Android credentials are
            accepted here.
          </p>
        </Panel>
      )}
      <div className="grid grid-cols-4 gap-3 max-xl:grid-cols-2 max-sm:grid-cols-1">
        {items.map(item => (
          <LiquidCard
            key={item.device_id}
            aria-pressed={item.device_id === deviceId}
            onClick={() => {
              select(item.device_id);
              setMessage('');
            }}
            className={`flex min-h-[145px] min-w-0 flex-col items-start rounded-2xl border p-5 text-left ${item.device_id === deviceId ? 'border-white/40 bg-[#292a30]' : 'border-white/10 bg-[#17181b]'}`}
          >
            <span aria-hidden="true" className="text-3xl">
              ⌘
            </span>
            <b className="mt-5 w-full break-words text-sm">{item.device_name}</b>
            <p className={`mt-2 ${muted}`}>Active pairing · {item.device_id.slice(0, 8)}</p>
          </LiquidCard>
        ))}
      </div>
      {deviceId && (
        <Panel key={deviceId} cls="th-enter mt-5 p-6">
          <h2 className="font-semibold">Pairing details</h2>
          {detail.isPending && connected && <Loading label="Loading pairing details…" />}
          {detail.isError && (
            <Notice error retry={connected ? () => void detail.refetch() : undefined}>
              {controlError(detail.error, 'read pairing details')}
            </Notice>
          )}
          {pairing && (
            <>
              <Row
                main={pairing.device_name}
                sub={pairing.device_id}
                right={<Chip>ACTIVE PAIRING</Chip>}
              />
              <Row main="Paired" sub={formatDate(pairing.created_at)} />
              <Row main="Expires" sub={formatDate(pairing.expires_at)} />
              <p className={`my-3 ${muted}`}>
                Web v1 does not expose network presence, discovery, enrollment or permission
                editing.
              </p>
              <button
                type="button"
                className={outline}
                onClick={() => {
                  revoke.reset();
                  setConfirm(pairing);
                }}
                disabled={!connected || devices.isFetching}
              >
                Revoke device
              </button>
            </>
          )}
        </Panel>
      )}
      {confirm && (
        <Modal
          title="Revoke paired device?"
          close={() => setConfirm(undefined)}
          locked={revoke.isPending}
        >
          <p className={`break-words ${muted}`}>
            Revoking {confirm.device_name} invalidates its existing companion access, not just the
            Web view. It must be paired again outside the browser to regain access.
          </p>
          {revoke.isError && (
            <Notice error>
              {controlError(revoke.error, 'revoke the device')} Refresh the list if the outcome is
              uncertain.
            </Notice>
          )}
          <div className="mt-4 flex flex-wrap gap-2">
            <button
              type="button"
              data-dialog-focus
              className={outline}
              disabled={revoke.isPending}
              onClick={() => setConfirm(undefined)}
            >
              Keep paired
            </button>
            <button
              type="button"
              className={solid}
              disabled={!connected || revoke.isPending}
              onClick={() => revoke.mutate(confirm.device_id)}
            >
              {revoke.isPending ? 'Revoking…' : 'Confirm revocation'}
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}
