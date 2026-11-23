import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { mayHaveBeenAccepted, type WorkspaceApi } from '../api/client';
import type { ApprovalInput, PairedDevice } from '../api/control';
import { formatDate, muted, outline, solid } from '../components/styles';
import { Chip, Loading, Modal, Notice, Panel, Row } from '../components/ui';
import { approvalsKey, controlError, tasksKey, useApprovals } from './controlState';
import { DevicePicker } from './DevicePicker';

function ApprovalReview({
  api,
  id,
  devices,
  connected,
  close,
}: {
  api: WorkspaceApi;
  id: string;
  devices: PairedDevice[];
  connected: boolean;
  close: () => void;
}) {
  const cache = useQueryClient();
  const [device, selectDevice] = useState('');
  const [expired, setExpired] = useState(false);
  const detail = useQuery({
    queryKey: ['web-control', 'approval', id],
    queryFn: ({ signal }) => api.getApproval(id, signal),
    staleTime: 0,
    enabled: connected,
  });
  const approval = detail.isSuccess ? detail.data : undefined;
  const expiry = approval?.expires_at;
  useEffect(() => {
    if (!expiry) return;
    const timer = setTimeout(() => setExpired(true), Math.max(0, Date.parse(expiry) - Date.now()));
    return () => clearTimeout(timer);
  }, [expiry]);

  const decision = useMutation({
    mutationFn: (input: ApprovalInput) => api.decideApproval(id, input),
    onSettled: () => {
      void detail.refetch();
      void cache.invalidateQueries({ queryKey: approvalsKey });
      void cache.invalidateQueries({ queryKey: tasksKey });
    },
  });
  const deviceId = devices.some(item => item.device_id === device)
    ? device
    : devices.length === 1
      ? devices[0].device_id
      : '';
  const uncertain = decision.isError && mayHaveBeenAccepted(decision.error);
  const sameDecision =
    uncertain &&
    decision.variables?.expected_operation_digest === approval?.operation_digest &&
    decision.variables?.task_id === approval?.task_id &&
    decision.variables?.requester_device_id === deviceId;
  const canDecide =
    connected &&
    !detail.isFetching &&
    !decision.isPending &&
    !decision.isSuccess &&
    !expired &&
    approval?.status === 'PENDING' &&
    Boolean(approval.canonical_arguments && deviceId);

  function decide(choice: ApprovalInput['decision']) {
    if (!canDecide || !approval || Date.parse(approval.expires_at) <= Date.now()) return;
    if (uncertain) {
      if (sameDecision && decision.variables?.decision === choice)
        decision.mutate(decision.variables);
      return;
    }
    decision.mutate({
      decision: choice,
      task_id: approval.task_id,
      expected_operation_digest: approval.operation_digest,
      requester_device_id: deviceId,
      idempotency_key: crypto.randomUUID(),
    });
  }

  return (
    <Modal
      title="Review exact operation"
      close={close}
      locked={decision.isPending}
      cls="max-w-[540px]"
    >
      {detail.isPending && connected && <Loading label="Loading approval details…" />}
      {detail.isError && (
        <Notice error retry={connected ? () => void detail.refetch() : undefined}>
          {controlError(detail.error, 'read the approval')}
        </Notice>
      )}
      {approval && (
        <>
          <dl className="space-y-2 break-words text-xs leading-5">
            <div>
              <dt className="font-semibold">Operation</dt>
              <dd>{approval.operation}</dd>
            </div>
            <div>
              <dt className="font-semibold">Target</dt>
              <dd>{approval.target_resource}</dd>
            </div>
            <div>
              <dt className="font-semibold">Task ID</dt>
              <dd className="break-all">{approval.task_id}</dd>
            </div>
            {approval.reason && (
              <div>
                <dt className="font-semibold">Reason</dt>
                <dd>{approval.reason}</dd>
              </div>
            )}
            <div>
              <dt className="font-semibold">Status and expiry</dt>
              <dd>
                {approval.status} · {formatDate(approval.expires_at)}
              </dd>
            </div>
            <div>
              <dt className="font-semibold">Operation SHA-256</dt>
              <dd className="break-all">{approval.operation_digest}</dd>
            </div>
            <div>
              <dt className="font-semibold">Arguments (as returned for review)</dt>
              <dd>
                {approval.canonical_arguments ? (
                  <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-xl bg-white/[.06] p-3">
                    {JSON.stringify(approval.canonical_arguments, null, 2)}
                  </pre>
                ) : (
                  'Exact operation arguments are unavailable. No decision can be made here.'
                )}
              </dd>
            </div>
          </dl>
          {expired && approval.status === 'PENDING' && (
            <p className={muted}>This request has expired; refresh to see the server state.</p>
          )}
          {approval.status === 'PENDING' && (
            <div className="mt-4">
              <DevicePicker
                devices={devices}
                value={deviceId}
                change={selectDevice}
                disabled={!connected || decision.isPending || uncertain}
                label="Approval requester device"
              />
              {devices.length > 1 && (
                <p className={`mt-2 ${muted}`}>
                  Choose the device that owns this task. Web v1 does not include its ID in approval
                  details; the server rejects any mismatch.
                </p>
              )}
            </div>
          )}
        </>
      )}
      {decision.isError && (
        <div className="mt-3">
          <Notice error>
            {controlError(decision.error, 'record the decision')}
            {uncertain &&
              ' The outcome is unconfirmed. The server status has been requested again; only the same decision can be retried with its original key.'}
          </Notice>
        </div>
      )}
      {decision.data && (
        <div className="mt-3">
          <Notice error={decision.data.success === false && decision.data.status !== 'REJECTED'}>
            Decision response: {decision.data.status}.
            {decision.data.execution_state && ` Execution: ${decision.data.execution_state}.`}
            {decision.data.executed === false && ' No execution reported.'}
            {decision.data.executed === true && ' The backend reports that the operation ran.'}
            {' Check the task for the persisted outcome.'}
          </Notice>
        </div>
      )}
      {approval?.status === 'PENDING' && (
        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="button"
            className={outline}
            disabled={
              !canDecide ||
              (uncertain && (!sameDecision || decision.variables?.decision !== 'reject'))
            }
            onClick={() => decide('reject')}
          >
            Reject this operation
          </button>
          <button
            type="button"
            className={solid}
            disabled={
              !canDecide ||
              (uncertain && (!sameDecision || decision.variables?.decision !== 'approve'))
            }
            onClick={() => decide('approve')}
          >
            Approve this operation
          </button>
        </div>
      )}
    </Modal>
  );
}

export function Approvals({
  api,
  devices,
  connected,
}: {
  api: WorkspaceApi;
  devices: PairedDevice[];
  connected: boolean;
}) {
  const list = useApprovals(api, connected);
  const [selected, select] = useState<string>();
  return (
    <Panel cls="min-w-0 p-5">
      <div className="flex flex-wrap justify-between gap-2">
        <h2 className="font-semibold">Review and approvals</h2>
        <button
          type="button"
          className={outline}
          disabled={!connected || list.isFetching}
          onClick={() => void list.refetch()}
        >
          Refresh approvals
        </button>
      </div>
      {list.isPending && connected && <Loading label="Loading pending approvals…" />}
      {list.isError && <Notice error>{controlError(list.error, 'list approvals')}</Notice>}
      {list.isSuccess && list.data.count === 0 && (
        <p className={`mt-3 ${muted}`}>No pending approvals.</p>
      )}
      {list.isSuccess &&
        list.data.approvals.map(item => (
          <Row
            key={item.id}
            main={item.operation}
            sub={`${item.target_resource} · Expires ${formatDate(item.expires_at)}`}
            right={
              <div className="flex flex-wrap gap-2">
                <Chip>{item.status}</Chip>
                <button
                  type="button"
                  className={outline}
                  disabled={!connected}
                  onClick={() => select(item.id)}
                >
                  Review approval
                </button>
              </div>
            }
          />
        ))}
      {selected && (
        <ApprovalReview
          key={selected}
          api={api}
          id={selected}
          devices={devices}
          connected={connected}
          close={() => select(undefined)}
        />
      )}
    </Panel>
  );
}
