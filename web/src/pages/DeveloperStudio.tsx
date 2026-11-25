import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Markdown from 'react-markdown';
import { mayHaveBeenAccepted, type WorkspaceApi } from '../api/client';
import { type PairedDevice, type TaskInput, type TaskScope } from '../api/control';
import { field, muted, outline, solid } from '../components/styles';
import { Chip, Head, Loading, Modal, Notice, Panel } from '../components/ui';
import { Approvals } from './Approvals';
import { controlError, taskHref, tasksKey, useDevices, useTasks } from './controlState';
import { DevicePicker } from './DevicePicker';
import { TaskDetail } from './TaskDetail';
import { readinessError, taskReadinessBlocker } from './supervisorReadiness';

function TaskComposer({
  api,
  devices,
  prefill,
  connected,
  onAccepted,
}: {
  api: WorkspaceApi;
  devices: PairedDevice[];
  prefill: string;
  connected: boolean;
  onAccepted: (taskId: string) => void;
}) {
  const cache = useQueryClient();
  const [objective, setObjective] = useState(prefill);
  const [selection, setSelection] = useState('');
  const [scope, setScope] = useState<TaskScope>('read_only');
  const [reviewing, setReviewing] = useState(Boolean(prefill));
  const [checking, setChecking] = useState(false);
  const [preflightError, setPreflightError] = useState('');
  const submitLock = useRef(false);
  const composer = useRef<HTMLTextAreaElement>(null);
  const alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    []
  );
  useEffect(() => {
    setObjective(prefill);
    if (prefill) setReviewing(true);
  }, [prefill]);
  const readiness = useQuery({
    queryKey: ['task-form-readiness'],
    queryFn: ({ signal }) => api.supervisorReadiness(signal),
    enabled: connected,
    retry: false,
  });
  const blocker = readiness.isSuccess ? taskReadinessBlocker(readiness.data) : undefined;
  const verified = readiness.isSuccess && !readiness.isFetching && !blocker;
  const submit = useMutation({
    mutationFn: (body: TaskInput) => api.submitTask(body),
    onSettled: () => {
      submitLock.current = false;
    },
    onSuccess: () => void cache.invalidateQueries({ queryKey: tasksKey }),
  });
  const deviceId = devices.some(item => item.device_id === selection)
    ? selection
    : devices.length === 1
      ? devices[0].device_id
      : '';
  const unavailable =
    submit.isError &&
    controlError(submit.error, 'submit') === 'Supervisor task worker is unavailable';
  const uncertain = submit.isError && !unavailable && mayHaveBeenAccepted(submit.error);
  const selectedDevice = devices.find(device => device.device_id === deviceId);

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!connected || !deviceId || !objective.trim() || !verified || submitLock.current) return;
    const body = uncertain
      ? submit.variables?.requester_device_id === deviceId
        ? submit.variables
        : undefined
      : {
          objective: objective.trim(),
          requester_device_id: deviceId,
          capability_scope: [scope],
          idempotency_key: crypto.randomUUID(),
        };
    if (!body) return;
    submitLock.current = true;
    setChecking(true);
    setPreflightError('');
    const fresh = await readiness.refetch();
    if (!alive.current) return;
    const problem = fresh.isSuccess
      ? taskReadinessBlocker(fresh.data)
      : readinessError(fresh.error);
    setChecking(false);
    if (problem) {
      setPreflightError(problem);
      submitLock.current = false;
      return;
    }
    submit.mutate(body, { onSuccess: data => onAccepted(data.task_id) });
  }

  return (
    <Panel cls="th-task-composer mb-5 p-4">
      <form onSubmit={send}>
        <label className="block text-xs font-medium" htmlFor="task-objective">
          What should Thozha do?
        </label>
        <div className="mt-2 flex items-end gap-3 max-md:flex-col max-md:items-stretch">
          <textarea
            ref={composer}
            id="task-objective"
            required
            maxLength={20_000}
            rows={2}
            className={`${field} flex-1 resize-y`}
            value={objective}
            placeholder="Check my Mac's disk space"
            onChange={event => setObjective(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <button
            type="button"
            className={outline}
            aria-expanded={reviewing}
            onClick={() => setReviewing(open => !open)}
          >
            {reviewing ? 'Hide review' : 'Review & submit'}
          </button>
        </div>
        <p className={`mt-2 ${muted}`}>
          ⌘/Ctrl+Enter submits after authorization review. A task is never submitted just by writing
          or navigating here.
        </p>
        {reviewing && (
          <div className="mt-4 border-t border-white/10 pt-4">
            <div className="grid grid-cols-[minmax(0,1fr)_220px] gap-4 max-lg:grid-cols-1">
              <div>
                <p className="text-xs font-medium">Exact instruction</p>
                <blockquote className="mt-2 whitespace-pre-wrap rounded-lg bg-white/[.04] p-3 text-xs">
                  {objective.trim() || 'Add an instruction above.'}
                </blockquote>
              </div>
              <div className="space-y-3">
                <DevicePicker devices={devices} value={deviceId} change={setSelection} />
                <label className="block text-xs">
                  Capability scope
                  <select
                    className={`${field} mt-2`}
                    value={scope}
                    onChange={event => {
                      const next = event.target.value;
                      if (next === 'read_only' || next === 'assistant' || next === 'development')
                        setScope(next);
                    }}
                  >
                    <option value="read_only">Read-only</option>
                    <option value="assistant">Assistant — approval gated</option>
                    <option value="development">Development — allowlisted</option>
                  </select>
                </label>
              </div>
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Chip>{selectedDevice ? selectedDevice.device_name : 'DEVICE REQUIRED'}</Chip>
              <Chip>{scope.replace(/_/g, ' ')}</Chip>
              <Chip>{verified ? 'PRECHECK READY' : 'PRECHECK INCOMPLETE'}</Chip>
            </div>
            <p className={`mt-3 ${muted}`}>
              The host still authorizes every operation. Sensitive or unapproved actions can pause
              for your explicit decision.
            </p>
            {connected && (readiness.isFetching || checking) && (
              <p role="status" className={`mt-3 ${muted}`}>
                {checking
                  ? 'Rechecking readiness before submission…'
                  : 'Checking supervisor readiness…'}
              </p>
            )}
            {connected && (readiness.isError || blocker || preflightError) && (
              <div className="mt-3">
                <Notice
                  error
                  retry={() => {
                    setPreflightError('');
                    void readiness.refetch();
                  }}
                >
                  {preflightError || blocker || readinessError(readiness.error)}
                </Notice>
              </div>
            )}
            {submit.isError && (
              <div className="mt-3">
                <Notice error>
                  {controlError(submit.error, 'submit the task')}
                  {uncertain &&
                    ' Acceptance is unconfirmed. Retry uses the original key; check history before starting another request.'}
                </Notice>
              </div>
            )}
            <button
              type="submit"
              className={`${solid} mt-4`}
              disabled={
                !connected ||
                !deviceId ||
                !objective.trim() ||
                !verified ||
                checking ||
                submit.isPending
              }
            >
              {checking
                ? 'Checking readiness…'
                : submit.isPending
                  ? 'Submitting…'
                  : uncertain
                    ? 'Retry same task'
                    : 'Submit reviewed task'}
            </button>
          </div>
        )}
      </form>
    </Panel>
  );
}

function taskGroup(state: string) {
  if (state === 'WAITING_APPROVAL') return 'Approvals';
  if (state === 'FAILED') return 'Failed';
  if (state === 'COMPLETED' || state === 'CANCELLED') return 'Completed';
  return 'Active';
}

export default function DeveloperStudio({
  api,
  connected,
  taskId,
  draft,
  clearDraft,
  view = 'developer',
}: {
  api: WorkspaceApi;
  connected: boolean;
  taskId?: string;
  draft?: string;
  clearDraft: () => void;
  view?: 'tasks' | 'developer';
}) {
  const [filter, setFilter] = useState<'active' | 'completed' | 'all'>('all');
  const [composerOpen, setComposerOpen] = useState(false);
  const devices = useDevices(api, connected);
  const tasks = useTasks(api, filter, connected);
  const approvals = useQuery({
    queryKey: ['web-control', 'approvals'],
    queryFn: ({ signal }) => api.listApprovals(signal),
    enabled: connected && view === 'tasks',
  });
  const mapped = devices.isSuccess ? devices.data.devices : [];
  const rows = tasks.data?.tasks ?? [];
  const counts = rows.reduce<Record<string, number>>((result, item) => {
    result[taskGroup(item.state)] = (result[taskGroup(item.state)] ?? 0) + 1;
    return result;
  }, {});
  const accepted = (id: string) => {
    setComposerOpen(false);
    clearDraft();
    window.location.hash = taskHref(id);
  };

  return (
    <div className={view === 'tasks' ? 'th-tasks-page' : undefined}>
      <Head
        label={view === 'tasks' ? 'SUPERVISOR / AUTHORIZED WORK' : 'EXECUTION / EVIDENCE'}
        title={view === 'tasks' ? 'Tasks' : 'Developer Studio'}
        desc={
          view === 'tasks'
            ? 'Start useful work, follow server-saved execution, and review results.'
            : 'Inspect persisted supervisor activity and execution evidence.'
        }
        action={
          <button type="button" className={solid} onClick={() => setComposerOpen(true)}>
            New task
          </button>
        }
      />
      {(composerOpen || Boolean(draft)) && (
        <Modal
          title="New task"
          cls="max-w-[720px]"
          close={() => {
            setComposerOpen(false);
            clearDraft();
          }}
        >
          <TaskComposer
            api={api}
            devices={mapped}
            connected={connected}
            prefill={draft ?? ''}
            onAccepted={accepted}
          />
        </Modal>
      )}
      {view === 'developer' && (
        <p className={`${muted} th-v4-diagnostics-note`}>
          GitHub and CI are not connected in Web v1. The timeline contains only server-saved
          supervisor work.
        </p>
      )}
      {view === 'tasks' && (
        <div className="th-task-stats" aria-label="Task summary">
          {[
            ['Active', counts.Active ?? 0],
            ['Pending approvals', approvals.data?.count ?? 0],
            ['Completed / cancelled', counts.Completed ?? 0],
            ['Failed', counts.Failed ?? 0],
          ].map(([label, value]) => (
            <div key={label}>
              <span>{label}</span>
              <b>{value}</b>
            </div>
          ))}
        </div>
      )}
      <div
        className={
          view === 'tasks'
            ? `th-task-workspace ${taskId ? 'has-selection' : ''}`
            : 'grid grid-cols-[minmax(0,1.5fr)_minmax(280px,1fr)] gap-4 max-lg:grid-cols-1'
        }
      >
        <Panel cls="th-task-history min-w-0">
          <div className="th-task-history-toolbar">
            <h2 className="font-semibold">Task history</h2>
            <div className="flex items-center gap-2">
              <label className="sr-only" htmlFor="task-filter">
                Task history filter
              </label>
              <select
                id="task-filter"
                className={`${field} !py-2`}
                value={filter}
                onChange={event => {
                  const next = event.target.value;
                  if (next === 'active' || next === 'completed' || next === 'all') setFilter(next);
                }}
              >
                <option value="all">All recent</option>
                <option value="active">Active</option>
                <option value="completed">Completed / failed / cancelled</option>
              </select>
              <button
                type="button"
                className={outline}
                disabled={!connected || tasks.isFetching}
                onClick={() => void tasks.refetch()}
              >
                Refresh
              </button>
            </div>
          </div>
          <div
            className="th-task-history-scroll"
            role="region"
            aria-label="Task history"
            tabIndex={0}
          >
            {tasks.isPending && connected && <Loading label="Loading persisted tasks…" />}
            {tasks.isError && (
              <div className="mt-3">
                <Notice error retry={connected ? () => void tasks.refetch() : undefined}>
                  {controlError(tasks.error, 'list supervisor tasks')}
                </Notice>
              </div>
            )}
            {tasks.isSuccess && !rows.length && (
              <p className={`py-10 text-center ${muted}`}>
                No saved tasks in this view. Submitted tasks will appear here.
              </p>
            )}
            <ul>
              {rows.map(item => (
                <li key={item.task_id}>
                  <a
                    href={taskHref(item.task_id)}
                    className="th-task-row"
                    data-task-state={item.state}
                    title={item.summary || `Task ${item.task_id}`}
                    aria-current={item.task_id === taskId ? 'true' : undefined}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="th-task-title">
                          <Markdown
                            allowedElements={['strong', 'em', 'code']}
                            unwrapDisallowed
                            skipHtml
                          >
                            {item.summary ||
                              `${item.state.charAt(0)}${item.state.slice(1).toLowerCase().replace(/_/g, ' ')} task`}
                          </Markdown>
                        </div>
                        <span className="th-task-meta" title={item.task_id}>
                          {item.state.replace(/_/g, ' ').toLowerCase()} · {item.task_id.slice(0, 8)}
                          <span className="sr-only"> · Task ID {item.task_id}</span>
                        </span>
                      </div>
                      <Chip>{taskGroup(item.state)}</Chip>
                    </div>
                  </a>
                </li>
              ))}
            </ul>
          </div>
        </Panel>
        {view === 'tasks' ? (
          taskId ? (
            <Panel cls="th-task-detail min-w-0">
              <div className="th-task-detail-toolbar">
                <h2>Execution details</h2>
                <a className={outline} href="#/tasks">
                  Back to history
                </a>
              </div>
              <div
                className="th-task-detail-scroll"
                role="region"
                aria-label="Execution details"
                tabIndex={0}
              >
                {taskId ? (
                  <TaskDetail key={taskId} api={api} taskId={taskId} connected={connected} />
                ) : (
                  <div className="th-task-empty">
                    <div>
                      <h2 className="font-semibold">Execution detail</h2>
                      <p className={`mt-2 ${muted}`}>
                        Select a saved task to see only the milestones, output and artifacts
                        reported by the host.
                      </p>
                    </div>
                  </div>
                )}
              </div>
            </Panel>
          ) : (
            <p className="th-task-selection-hint">
              Select a task to inspect its saved progress, output, and artifacts.
            </p>
          )
        ) : (
          <Approvals api={api} devices={mapped} connected={connected} />
        )}
      </div>
    </div>
  );
}
