import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { QRCodeSVG } from 'qrcode.react';
import { encodeCompactPairingQr } from '../lib/compactPairingQr';
import type {
  PairingDecision,
  PairingInvitation,
  PairingInvitationStatus,
  PairingRequest,
} from '../api/control';
import { WebApiError, type WorkspaceApi } from '../api/client';
import { formatDate, muted, outline, solid } from '../components/styles';
import { Chip, Modal, Notice, Panel, Row } from '../components/ui';
import { devicesKey, controlError } from './controlState';

const pairingRequestsKey = ['web-control', 'pairing-requests'] as const;
const invitationKey = (id: string) => ['web-control', 'pairing-invitation', id] as const;
const terminalInvitationStates = new Set<PairingInvitationStatus['status']>([
  'REJECTED',
  'EXPIRED',
  'CANCELLED',
  'TOKEN_ISSUED',
]);

const invitationStatusText: Record<PairingInvitationStatus['status'], string> = {
  INVITED: 'Waiting for the phone to scan the invitation.',
  PENDING_CONFIRMATION: 'The phone proved its key. Review the pending request below.',
  APPROVED: 'Owner approval recorded. Waiting for the phone to complete its key-proof claim.',
  REJECTED: 'The pairing request was rejected.',
  EXPIRED: 'This invitation expired and can no longer be used.',
  CANCELLED: 'This invitation was cancelled.',
  TOKEN_ISSUED: 'The phone completed pairing. Web task access still requires separate mapping.',
};

function remainingText(milliseconds: number) {
  const totalSeconds = Math.max(0, Math.ceil(milliseconds / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  return `${minutes}:${String(totalSeconds % 60).padStart(2, '0')}`;
}

function PairingReview({
  request,
  close,
  decide,
  pending,
  error,
}: {
  request: PairingRequest;
  close: () => void;
  decide: (decision: PairingDecision) => void;
  pending: boolean;
  error?: string;
}) {
  return (
    <Modal title="Review pairing request" close={close} locked={pending} cls="max-w-[540px]">
      <p className={muted}>
        Approve only a phone you recognize. Verify its fingerprint with the person pairing it.
        Approval creates a short-lived claim challenge; the phone must still prove key possession.
      </p>
      <dl className="mt-4 space-y-3 break-words text-xs leading-5">
        <div>
          <dt className="font-semibold">Device name</dt>
          <dd>{request.device_name}</dd>
        </div>
        <div>
          <dt className="font-semibold">Verified device fingerprint</dt>
          <dd className="break-all font-mono">{request.device_fingerprint}</dd>
        </div>
        <div>
          <dt className="font-semibold">Request status</dt>
          <dd>{request.status}</dd>
        </div>
        <div>
          <dt className="font-semibold">Requested</dt>
          <dd>{request.requested_at ? formatDate(request.requested_at) : 'Not reported'}</dd>
        </div>
        <div>
          <dt className="font-semibold">Expires</dt>
          <dd>{formatDate(request.expires_at)}</dd>
        </div>
      </dl>
      <p className={`mt-4 ${muted}`}>
        Pairing does not grant Web task authorization. The host owner must separately map an active
        device for Web tasks.
      </p>
      {error && (
        <div className="mt-4">
          <Notice error>{error}</Notice>
        </div>
      )}
      <div className="mt-5 flex flex-wrap justify-end gap-2">
        <button
          type="button"
          className={outline}
          data-dialog-focus
          disabled={pending}
          onClick={close}
        >
          Not now
        </button>
        <button
          type="button"
          className={outline}
          disabled={pending}
          onClick={() => decide('reject')}
        >
          {pending ? 'Recording…' : 'Reject request'}
        </button>
        <button
          type="button"
          className={solid}
          disabled={pending}
          onClick={() => decide('approve')}
        >
          {pending ? 'Recording…' : 'Approve pairing'}
        </button>
      </div>
    </Modal>
  );
}

export default function DevicePairing({
  api,
  connected,
}: {
  api: WorkspaceApi;
  connected: boolean;
}) {
  const cache = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [invitation, setInvitation] = useState<PairingInvitation>();
  const [review, setReview] = useState<PairingRequest>();
  const [notice, setNotice] = useState('');
  const [payloadCopyNotice, setPayloadCopyNotice] = useState('');
  const [now, setNow] = useState(() => Date.now());
  const [setupOpen, setSetupOpen] = useState(false);
  const requests = useQuery({
    queryKey: pairingRequestsKey,
    queryFn: ({ signal }) => api.listPairingRequests(50, signal),
    enabled: connected,
    refetchInterval: connected ? 5000 : false,
    refetchIntervalInBackground: false,
  });
  const invitationStatus = useQuery({
    queryKey: invitation
      ? invitationKey(invitation.invitation_id)
      : ['web-control', 'no-invitation'],
    queryFn: ({ signal }) => api.getPairingInvitationStatus(invitation!.invitation_id, signal),
    enabled: connected && Boolean(invitation),
    refetchInterval: query => {
      const state = query.state.data?.status;
      return state && terminalInvitationStates.has(state) ? false : 2000;
    },
    refetchIntervalInBackground: false,
  });
  const create = useMutation({
    mutationFn: () => api.createPairingInvitation(),
    onSuccess: result => {
      setInvitation(result);
      setNotice('');
      setNow(Date.now());
    },
  });
  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelPairingInvitation(id),
    onSuccess: result => {
      const status: PairingInvitationStatus = {
        invitation_id: result.invitation_id,
        status: result.status,
      };
      cache.setQueryData(invitationKey(result.invitation_id), status);
      setNotice(
        result.status === 'CANCELLED'
          ? 'Pairing invitation cancelled.'
          : `The invitation is ${result.status.toLowerCase()}.`
      );
      void invitationStatus.refetch();
    },
  });
  const decision = useMutation({
    mutationFn: ({ id, choice }: { id: string; choice: PairingDecision }) =>
      api.decidePairingRequest(id, choice),
    onSuccess: result => {
      setReview(undefined);
      setNotice(
        result.status === 'APPROVED'
          ? 'Owner approval recorded. The phone must still complete its key-proof claim. This does not grant Web task authorization.'
          : result.status === 'REJECTED'
            ? 'Pairing request rejected.'
            : `The pairing request is ${result.status.toLowerCase()}.`
      );
      void cache.invalidateQueries({ queryKey: pairingRequestsKey });
      void cache.invalidateQueries({ queryKey: devicesKey });
    },
    onError: () => {
      void cache.invalidateQueries({ queryKey: pairingRequestsKey });
    },
  });

  const activeInvitationId = invitation?.invitation_id;
  useEffect(() => {
    if (!activeInvitationId) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [activeInvitationId]);

  useEffect(() => {
    if (invitationStatus.data?.status !== 'TOKEN_ISSUED') return;
    void cache.invalidateQueries({ queryKey: devicesKey });
  }, [cache, invitationStatus.data?.status]);

  const remaining = invitation ? Math.max(0, Date.parse(invitation.expires_at) - now) : 0;
  const status = invitationStatus.data?.status ?? (invitation ? 'INVITED' : undefined);
  const hasActiveInvitation = Boolean(
    invitation && remaining > 0 && status && !terminalInvitationStates.has(status)
  );
  const visibleQr = Boolean(invitation && remaining > 0 && (status === 'INVITED' || !status));
  const gatewayUnavailable =
    create.error instanceof WebApiError &&
    create.error.status === 409 &&
    /not enabled|not configured|gateway|tls|certificate/i.test(create.error.detail ?? '');
  const networkUnavailable =
    create.error instanceof WebApiError &&
    (create.error.kind === 'connection' || create.error.kind === 'timeout');

  function openPairing() {
    setDialogOpen(true);
    setNotice('');
    setSetupOpen(false);
    create.reset();
    cancel.reset();
    if (!hasActiveInvitation) {
      setInvitation(undefined);
      create.mutate();
    }
  }

  return (
    <section aria-label="Secure device pairing" className="mb-5">
      <Panel cls="p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="text-[10px] font-semibold tracking-[.18em] text-[#a4a5af]">
              SECURE ONBOARDING
            </p>
            <h2 className="mt-1 text-base font-semibold">Pair a companion device</h2>
            <p className={`mt-2 max-w-3xl ${muted}`}>
              Connect your phone over an approved private connection, scan an invitation, and
              confirm its identity. Task permissions remain separate.
            </p>
          </div>
          <button
            type="button"
            className={solid}
            disabled={!connected || create.isPending}
            onClick={openPairing}
          >
            {hasActiveInvitation ? 'View invitation' : 'Pair new device'}
          </button>
        </div>

        {notice && (
          <div className="mt-4">
            <Notice>{notice}</Notice>
          </div>
        )}

        <div className="mt-5 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-medium">Pending phone requests</h3>
            <p className={muted}>Requests appear only after the phone proves its key.</p>
          </div>
          <button
            type="button"
            className={outline}
            disabled={!connected || requests.isFetching}
            onClick={() => void requests.refetch()}
          >
            Refresh requests
          </button>
        </div>
        {requests.isPending && connected && (
          <p role="status" className={`mt-3 ${muted}`}>
            Loading pairing requests…
          </p>
        )}
        {requests.isError && (
          <div className="mt-3">
            <Notice error retry={connected ? () => void requests.refetch() : undefined}>
              {controlError(requests.error, 'list pending pairing requests')}
            </Notice>
          </div>
        )}
        {requests.isSuccess && requests.data.requests.length === 0 && (
          <p className={`mt-3 ${muted}`}>No pending pairing requests.</p>
        )}
        {requests.isSuccess && requests.data.requests.length > 0 && (
          <ul className="mt-2 divide-y divide-white/[.08]">
            {requests.data.requests.map(request => (
              <li
                key={request.id}
                className="flex flex-wrap items-center justify-between gap-3 py-3"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{request.device_name}</p>
                  <p className={`mt-1 break-all font-mono text-[10px] ${muted}`}>
                    {request.device_fingerprint}
                  </p>
                  <p className={`mt-1 ${muted}`}>Expires {formatDate(request.expires_at)}</p>
                </div>
                <div className="flex items-center gap-2">
                  <Chip>PENDING CONFIRMATION</Chip>
                  <button
                    type="button"
                    className={outline}
                    disabled={!connected || decision.isPending}
                    onClick={() => {
                      decision.reset();
                      setReview(request);
                    }}
                  >
                    Review identity
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      {dialogOpen && (
        <Modal
          title="Pair new device"
          close={() => setDialogOpen(false)}
          locked={create.isPending || cancel.isPending}
          cls="max-w-[560px]"
        >
          <ol className="th-pairing-steps" aria-label="Pairing steps">
            {['Connect privately', 'Scan invitation', 'Approve phone'].map((step, index) => (
              <li
                key={step}
                aria-current={index === (invitation ? (visibleQr ? 1 : 2) : 0) ? 'step' : undefined}
              >
                <span aria-hidden="true">{index + 1}</span>
                {step}
              </li>
            ))}
          </ol>
          <p className={`${muted} mt-4`}>
            Your Mac and phone need an approved private connection. An invitation is available only
            when that connection has been securely configured.
          </p>
          {!connected && (
            <div className="mt-4">
              <Notice error>
                The Web session is not connected. Sign in again to create an invitation.
              </Notice>
            </div>
          )}
          {create.isPending && (
            <p role="status" className={`mt-4 ${muted}`}>
              Creating a signed invitation…
            </p>
          )}
          {create.isError && (
            <div className="th-pairing-unavailable">
              <h3 role="alert">
                {gatewayUnavailable
                  ? 'Private connection is not set up'
                  : networkUnavailable
                    ? 'Could not reach your Mac'
                    : 'Invitation unavailable'}
              </h3>
              <p>
                {gatewayUnavailable
                  ? 'Finish the private connection setup on your Mac before pairing this phone. No invitation has been created.'
                  : networkUnavailable
                    ? 'Check that your Mac is reachable and try again. No QR is available until the host confirms an invitation.'
                    : 'The host could not issue an invitation. Review the details below before trying again.'}
              </p>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  className={outline}
                  aria-expanded={setupOpen}
                  onClick={() => setSetupOpen(value => !value)}
                >
                  {setupOpen ? 'Hide setup requirements' : 'View setup requirements'}
                </button>
                <button
                  type="button"
                  className={outline}
                  disabled={!connected || create.isPending}
                  onClick={() => create.mutate()}
                >
                  Check connection
                </button>
              </div>
              {setupOpen && (
                <ul className="list-disc space-y-2 pl-4 text-xs leading-5 text-[#b8bac3]">
                  <li>Keep the Mac and phone on the owner-approved private network.</li>
                  <li>
                    The Mac host needs its private Wi-Fi gateway and trusted TLS certificate
                    configured and verified.
                  </li>
                  <li>
                    Once the host is ready, check the connection here and scan the new invitation in
                    the companion app.
                  </li>
                </ul>
              )}
              <details className="th-pairing-diagnostics">
                <summary>Connection diagnostics</summary>
                <p>{controlError(create.error, 'create a secure Wi-Fi pairing invitation')}</p>
                {create.error instanceof WebApiError && create.error.status && (
                  <p>HTTP {create.error.status}</p>
                )}
                <p>No QR code was created. Pairing fails closed; do not reuse an old invitation.</p>
              </details>
            </div>
          )}
          {invitation && (
            <div className="mt-4">
              {visibleQr ? (
                <div className="grid justify-items-center gap-3">
                  <div className="rounded-2xl bg-white p-3">
                    <QRCodeSVG
                      value={encodeCompactPairingQr(invitation.qr_payload)}
                      size={288}
                      className="th-pairing-qr"
                      level="L"
                      marginSize={2}
                      role="img"
                    aria-label="Signed Wi-Fi pairing invitation QR code"
                    />
                  </div>
                  <p className="text-sm font-medium">
                    Expires in <span>{remainingText(remaining)}</span>
                  </p>
                  <p className={`max-w-lg text-center text-xs ${muted}`}>
                    Scan this compact signed code inside Thozha. The private .local host has no verified Android App Link;
                    don’t send the code to search or another app.
                  </p>
                  <p className={`max-w-full break-all text-center font-mono text-[10px] ${muted}`}>
                    Host fingerprint: {invitation.server_fingerprint}
                  </p>
                  <button
                    type="button"
                    className={outline}
                    onClick={async () => {
                      try {
                        await navigator.clipboard.writeText(JSON.stringify(invitation.qr_payload));
                        setPayloadCopyNotice('Signed invitation copied. Paste it only into Thozha’s in-app pairing screen.');
                      } catch {
                        setPayloadCopyNotice('Clipboard access is unavailable. Select the signed JSON in Manual import below.');
                      }
                    }}
                  >
                    Copy signed invitation for manual import
                  </button>
                  {payloadCopyNotice && (
                    <p role="status" className={`max-w-lg text-center text-xs ${muted}`}>
                      {payloadCopyNotice}
                    </p>
                  )}
                  <details className="w-full max-w-lg rounded-xl border border-white/10 p-3">
                    <summary className="cursor-pointer text-sm font-medium">Manual import JSON</summary>
                    <p className={`mt-2 text-xs ${muted}`}>
                      This short-lived signed invitation contains no device token. Paste it only in the Thozha app; never into a search engine or unrelated site.
                    </p>
                    <textarea
                      aria-label="Signed pairing invitation JSON"
                      readOnly
                      value={JSON.stringify(invitation.qr_payload)}
                      className="mt-2 min-h-32 w-full select-all resize-y rounded-lg bg-black/30 p-2 font-mono text-[10px] text-[#d8d9df]"
                    />
                  </details>
                </div>
              ) : (
                <p role="status" className={muted}>
                  {remaining === 0
                    ? 'This QR invitation has expired.'
                    : status
                      ? invitationStatusText[status]
                      : 'The invitation status is being checked.'}
                </p>
              )}
              {invitationStatus.isError && (
                <div className="mt-3">
                  <Notice
                    error
                    retry={connected ? () => void invitationStatus.refetch() : undefined}
                  >
                    {controlError(invitationStatus.error, 'check the pairing invitation status')}
                  </Notice>
                </div>
              )}
              <Row
                main="Invitation expires"
                sub={formatDate(invitation.expires_at)}
                right={status && <Chip>{status.replace(/_/g, ' ')}</Chip>}
              />
              {status && !terminalInvitationStates.has(status) && remaining > 0 && (
                <button
                  type="button"
                  className={`${outline} mt-2`}
                  disabled={!connected || cancel.isPending}
                  onClick={() => cancel.mutate(invitation.invitation_id)}
                >
                  {cancel.isPending ? 'Cancelling…' : 'Cancel invitation'}
                </button>
              )}
              {cancel.isError && (
                <div className="mt-3">
                  <Notice error>
                    {controlError(cancel.error, 'cancel the pairing invitation')}
                  </Notice>
                </div>
              )}
            </div>
          )}
          <p className={`mt-5 ${muted}`}>
            Keep the QR private and scan it only with the intended phone. Pairing never
            automatically authorizes Web task execution.
          </p>
        </Modal>
      )}
      {review && (
        <PairingReview
          request={review}
          close={() => setReview(undefined)}
          decide={choice => decision.mutate({ id: review.id, choice })}
          pending={decision.isPending}
          error={
            decision.isError
              ? controlError(decision.error, 'record the pairing decision')
              : undefined
          }
        />
      )}
    </section>
  );
}
