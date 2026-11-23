import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { WorkspaceApi } from '../api/client';
import type { SupervisorArtifact, SupervisorEvent } from '../api/control';
import { matchesSha256 } from '../api/integrity';
import { formatDate, muted, outline, solid } from '../components/styles';
import { Chip, Loading, Modal, Notice, Row } from '../components/ui';
import { approvalsKey, controlError, httpStatus, tasksKey } from './controlState';
import { isTerminalTask, useTaskProgress } from './useTaskProgress';

function actualMilestones(events: SupervisorEvent[], state?: string) {
  const transitions = events
    .filter(item => item.event_type === 'task.state')
    .flatMap(item =>
      typeof item.payload.to === 'string' ? [{ state: item.payload.to, at: item.created_at }] : []
    );
  const reached = new Set(transitions.map(item => item.state));
  if (state) reached.add(state);
  return [
    ['Submitted', reached.has('CREATED'), transitions.find(item => item.state === 'CREATED')?.at],
    [
      'Authorization checked',
      reached.has('PLANNING'),
      transitions.find(item => item.state === 'PLANNING')?.at,
    ],
    [
      'Queued',
      reached.has('CREATED') && reached.has('RUNNING'),
      transitions.find(item => item.state === 'CREATED')?.at,
    ],
    ['Running', reached.has('RUNNING'), transitions.find(item => item.state === 'RUNNING')?.at],
    [
      'Awaiting approval',
      reached.has('WAITING_APPROVAL'),
      transitions.find(item => item.state === 'WAITING_APPROVAL')?.at,
    ],
    [
      'Terminal',
      reached.has('COMPLETED') || reached.has('FAILED') || reached.has('CANCELLED'),
      transitions.find(item => ['COMPLETED', 'FAILED', 'CANCELLED'].includes(item.state))?.at,
    ],
  ] as const;
}

function TaskResult({ text, digest }: { text: string; digest?: string | null }) {
  const [verification, setVerification] = useState<{
    text: string;
    digest: string;
    valid: boolean;
  }>();
  useEffect(() => {
    if (!digest) return;
    let active = true;
    void matchesSha256(new TextEncoder().encode(text), digest).then(
      valid => {
        if (active) setVerification({ text, digest, valid });
      },
      () => {
        if (active) setVerification({ text, digest, valid: false });
      }
    );
    return () => {
      active = false;
    };
  }, [digest, text]);
  const verified =
    verification?.text === text && verification?.digest === digest ? verification.valid : undefined;
  const truncated = text.endsWith(
    '[Jarvis stopped this answer at 256 KiB on the Mac. The rest was not stored.]'
  );
  return (
    <section className="mt-4 space-y-3">
      <h4 className="text-sm font-semibold">Saved result</h4>
      {!digest && <p className={muted}>No result digest was supplied by the server.</p>}
      {digest && verified === undefined && (
        <p role="status" className={muted}>
          Checking result SHA-256…
        </p>
      )}
      {verified === true && <Chip>RESULT SHA-256 VERIFIED</Chip>}
      {verified === false && (
        <Notice error>
          {truncated
            ? 'The host truncated the stored result; the digest covers the full original response, not this preview.'
            : 'The saved result could not be verified.'}
        </Notice>
      )}
      {(!digest || verified === true || (truncated && verified === false)) && (
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-xl border border-white/10 bg-white/[.04] p-3 text-xs leading-6">
          {text || 'The server returned an empty response.'}
        </pre>
      )}
      {digest && <p className={`break-all ${muted}`}>SHA-256: {digest}</p>}
    </section>
  );
}

function Artifacts({
  api,
  taskId,
  connected,
}: {
  api: WorkspaceApi;
  taskId: string;
  connected: boolean;
}) {
  const list = useQuery({
    queryKey: ['web-control', 'artifacts', taskId],
    queryFn: ({ signal }) => api.listArtifacts(taskId, signal),
    enabled: connected,
  });
  const download = useRef<AbortController | null>(null);
  const [working, setWorking] = useState<string>();
  const [message, setMessage] = useState<{ error: boolean; text: string }>();
  useEffect(() => () => download.current?.abort(), []);

  async function save(item: SupervisorArtifact) {
    if (download.current) return;
    const controller = new AbortController();
    download.current = controller;
    setWorking(item.artifact_id);
    setMessage(undefined);
    try {
      const zip = await api.downloadArtifact(item, controller.signal);
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(zip);
      const link = document.createElement('a');
      const name = item.file_name.replace(/[^a-zA-Z0-9._-]/g, '_');
      link.download = name.toLowerCase().endsWith('.zip') ? name : `${item.artifact_id}.zip`;
      link.href = url;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 30_000);
      setMessage({
        error: false,
        text: 'Archive verified against the server SHA-256 and sent to your downloads.',
      });
    } catch (error) {
      if (!controller.signal.aborted)
        setMessage({
          error: true,
          text: `${controlError(error, 'download the archive')} No file was saved.`,
        });
    } finally {
      if (download.current === controller) {
        download.current = null;
        if (!controller.signal.aborted) setWorking(undefined);
      }
    }
  }
  return (
    <div className="mt-3">
      {list.isPending && connected && <Loading label="Checking for authorized artifacts…" />}
      {list.isError &&
        (httpStatus(list.error) === 409 ? (
          <p className={muted}>
            No artifact collection is available for this task. Only completed development tasks can
            expose archives.
          </p>
        ) : (
          <Notice error retry={connected ? () => void list.refetch() : undefined}>
            {controlError(list.error, 'list artifacts')}
          </Notice>
        ))}
      {list.isSuccess && !list.data.artifacts.length && (
        <p className={muted}>No archives were published for this task.</p>
      )}
      {list.isSuccess &&
        list.data.artifacts.map(item => (
          <Row
            key={item.artifact_id}
            main={item.file_name}
            sub={`${item.size_bytes.toLocaleString()} bytes · ${item.files.length} files`}
            right={
              <button
                type="button"
                className={outline}
                disabled={!connected || Boolean(working)}
                onClick={() => void save(item)}
              >
                {working === item.artifact_id ? 'Verifying…' : 'Download ZIP'}
              </button>
            }
          />
        ))}
      {message && <Notice error={message.error}>{message.text}</Notice>}
    </div>
  );
}

function eventDetail(item: SupervisorEvent) {
  return [
    item.payload.to,
    item.payload.step,
    item.payload.operation,
    item.payload.target,
    item.payload.reason,
    item.payload.error_code,
  ]
    .filter((value): value is string => typeof value === 'string' && Boolean(value))
    .join(' · ');
}

export function TaskDetail({
  api,
  taskId,
  connected,
}: {
  api: WorkspaceApi;
  taskId: string;
  connected: boolean;
}) {
  const { task, timeline, mode, error, retry } = useTaskProgress(api, taskId, connected);
  const cache = useQueryClient();
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [showArtifacts, setShowArtifacts] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  const cancel = useMutation({
    mutationFn: () => api.cancelTask(taskId),
    onSuccess: () => {
      setConfirmCancel(false);
      void cache.invalidateQueries({ queryKey: tasksKey });
      void cache.invalidateQueries({ queryKey: approvalsKey });
      retry();
    },
  });
  const progress = !connected
    ? 'Offline · last received data'
    : mode === 'following'
      ? 'Following supervisor events'
      : mode === 'reconnecting'
        ? 'Reconnecting to persisted progress'
        : mode === 'history'
          ? 'Recorded history'
          : null;
  const terminal = task && isTerminalTask(task.state);
  const milestones = actualMilestones(timeline.events, task?.state);
  const duration =
    timeline.events.length > 1
      ? Math.max(
          0,
          Date.parse(timeline.events.at(-1)!.created_at) - Date.parse(timeline.events[0].created_at)
        )
      : undefined;
  const evidence = timeline.events.filter(item =>
    /tool|approval|operation|artifact|result|error/i.test(item.event_type)
  );
  return (
    <section
      aria-label="Supervisor task detail"
      className="my-4 min-w-0 space-y-3 rounded-xl border border-white/10 p-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 ref={heading} tabIndex={-1} className="text-sm font-semibold">
            Task detail
          </h3>
          <p className={`break-all ${muted}`}>{taskId}</p>
        </div>
        {task && <Chip>{task.state.replace(/_/g, ' ')}</Chip>}
      </div>
      {progress && (
        <p role="status" className={muted}>
          {progress}
        </p>
      )}
      {task && (
        <div className="mt-2">
          <h4 className="text-xs font-semibold">Verified lifecycle</h4>
          <ol
            className="mt-2 grid grid-cols-3 gap-2 max-sm:grid-cols-2"
            aria-label="Task lifecycle milestones"
          >
            {milestones.map(([label, reached, at]) => (
              <li
                key={label}
                className={`rounded-lg border p-3 text-xs ${reached ? 'border-white/25 bg-white/[.06]' : 'border-white/[.07] text-[#7f818b]'}`}
              >
                <b>
                  {reached ? '✓' : '○'} {label}
                </b>
                <span className="mt-1 block">
                  {at ? formatDate(at) : reached ? task.state.replace(/_/g, ' ') : 'Not reported'}
                </span>
              </li>
            ))}
          </ol>
          <p className={`mt-2 ${muted}`}>
            {duration == null
              ? 'Duration is unavailable.'
              : `Observed event duration: ${(duration / 1000).toFixed(1)} seconds.`}{' '}
            No progress percentage is inferred.
          </p>
        </div>
      )}
      {!task && !error && connected && <Loading label="Loading saved task…" />}
      {Boolean(error) && (
        <Notice error retry={connected ? retry : undefined}>
          {controlError(error, 'read task progress')}
        </Notice>
      )}
      {task?.state === 'FAILED' && (
        <Notice error>
          Supervisor task failed{task.error_code ? `: ${task.error_code}` : '.'}
        </Notice>
      )}
      {task?.state === 'WAITING_APPROVAL' && (
        <p className={muted}>
          Review the pending request in{' '}
          <a className="underline" href="#/approvals">
            Approvals
          </a>
          . No action is authorized by this timeline.
        </p>
      )}
      {task?.state === 'PAUSED' && (
        <p className={muted}>Paused by the supervisor. There is no Web task resume control.</p>
      )}
      {cancel.data && (
        <Notice>
          {cancel.data.cancelled
            ? 'Cancellation recorded.'
            : `Task is ${cancel.data.state.toLowerCase()} and was not cancelled.`}
          {cancel.data.action_may_have_started &&
            ' An approved action may already have started and cannot be undone here.'}
        </Notice>
      )}
      {task && !terminal && (
        <button
          type="button"
          className={outline}
          disabled={!connected || cancel.isPending}
          onClick={() => {
            cancel.reset();
            setConfirmCancel(true);
          }}
        >
          Cancel task
        </button>
      )}
      {task?.state === 'COMPLETED' && task.result_text != null && (
        <TaskResult
          key={task.result_digest ?? 'undigested'}
          text={task.result_text}
          digest={task.result_digest}
        />
      )}
      {task?.state === 'COMPLETED' && task.result_text == null && (
        <p className={muted}>
          {mode === 'history' ? 'No saved result text was returned.' : 'Fetching the saved result…'}
        </p>
      )}
      {task?.state === 'COMPLETED' && (
        <>
          <button
            type="button"
            className={outline}
            aria-expanded={showArtifacts}
            disabled={!connected && !showArtifacts}
            onClick={() => setShowArtifacts(open => !open)}
          >
            {showArtifacts ? 'Hide task artifacts' : 'Check task artifacts'}
          </button>
          {showArtifacts && <Artifacts api={api} taskId={taskId} connected={connected} />}
        </>
      )}
      {task && (
        <div>
          {evidence.length > 0 && (
            <div className="mb-4">
              <h4 className="text-xs font-semibold">Tool and authorization evidence</h4>
              <ul className="mt-2 space-y-2">
                {evidence.slice(-10).map(item => (
                  <li
                    key={item.seq}
                    className="rounded-lg border border-white/10 bg-white/[.03] p-3 text-xs"
                  >
                    <b>{item.event_type}</b>
                    <p className={`mt-1 ${muted}`}>
                      {eventDetail(item) || 'Saved event contained no displayable evidence.'}
                    </p>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <h4 className="text-xs font-semibold">Supervisor events</h4>
          {timeline.total === 0 && <p className={muted}>No events loaded yet.</p>}
          {timeline.total > timeline.events.length && (
            <p className={muted}>
              Showing the latest {timeline.events.length} of {timeline.total} events.
            </p>
          )}
          {timeline.events.length > 0 && (
            <ol
              className="mt-2 max-h-72 overflow-auto rounded-xl border border-white/10 px-3"
              tabIndex={0}
              aria-label="Saved task events"
            >
              {timeline.events.map(item => (
                <li key={item.seq} data-sequence={item.seq}>
                  <Row
                    main={item.event_type}
                    sub={[formatDate(item.created_at), eventDetail(item)]
                      .filter(Boolean)
                      .join(' · ')}
                  />
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
      {confirmCancel && (
        <Modal
          title="Cancel supervisor task?"
          close={() => setConfirmCancel(false)}
          locked={cancel.isPending}
        >
          <p className={muted}>
            This stops the remaining task. An operation already dispatched may not be reversible.
          </p>
          {cancel.isError && <Notice error>{controlError(cancel.error, 'cancel the task')}</Notice>}
          <div className="mt-4 flex flex-wrap gap-2">
            <button
              type="button"
              data-dialog-focus
              className={outline}
              disabled={cancel.isPending}
              onClick={() => setConfirmCancel(false)}
            >
              Keep task
            </button>
            <button
              type="button"
              className={solid}
              disabled={!connected || cancel.isPending || terminal}
              onClick={() => cancel.mutate()}
            >
              {cancel.isPending ? 'Cancelling…' : 'Confirm cancellation'}
            </button>
          </div>
        </Modal>
      )}
    </section>
  );
}
