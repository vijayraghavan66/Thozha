import { useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Chip, Head, Line, Modal, Notice, Panel, Row, Loading, Unavailable } from '../components/ui';
import { field, formatDate, muted, outline, solid } from '../components/styles';
import { type WorkspaceApi } from '../api/client';
import {
  type SupervisorTask,
  type SupervisorEvent,
  type TaskInput,
} from '../api/control';
import { approvalsKey, controlError, tasksKey } from './controlState';
import { isTerminalTask, useTaskProgress } from './useTaskProgress';
import { MarkdownMessage } from '../chat/MarkdownMessage';

type TaskFilter = 'all' | 'active' | 'completed' | 'waiting-approval' | 'failed';

function TaskList({
  api,
  connected,
  onSelect,
  filter,
  setFilter,
}: {
  api: WorkspaceApi;
  connected: boolean;
  onSelect: (taskId: string) => void;
  filter: TaskFilter;
  setFilter: (f: TaskFilter) => void;
}) {
  const list = useQuery({
    queryKey: [...tasksKey, filter],
    queryFn: ({ signal }) => {
      const stateMap: Record<TaskFilter, 'active' | 'completed' | 'all'> = {
        all: 'all',
        active: 'active',
        completed: 'completed',
        'waiting-approval': 'active',
        failed: 'active',
      };
      return api.listTasks(stateMap[filter], 100, signal);
    },
    enabled: connected,
  });

  const filtered = (list.data?.tasks ?? []).filter(task => {
    if (filter === 'waiting-approval') return task.state === 'WAITING_APPROVAL';
    if (filter === 'failed') return task.state === 'FAILED';
    if (filter === 'completed') return task.state === 'COMPLETED';
    return true;
  });

  return (
    <Panel cls="p-5">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-semibold">Supervisor tasks</h2>
        <div className="flex flex-wrap gap-2">
          {(['all', 'active', 'waiting-approval', 'failed', 'completed'] as TaskFilter[]).map(f => (
            <button
              key={f}
              type="button"
              className={f === filter ? solid : outline}
              onClick={() => setFilter(f)}
            >
              {f.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}
            </button>
          ))}
          <button
            type="button"
            className={outline}
            disabled={!connected || list.isFetching}
            onClick={() => void list.refetch()}
          >
            Refresh
          </button>
        </div>
      </div>
      {list.isPending && connected && <Loading label="Loading supervisor tasks…" />}
      {list.isError && <Notice error>{controlError(list.error, 'list supervisor tasks')}</Notice>}
      {list.isSuccess && filtered.length === 0 && (
        <p className={`py-10 text-center ${muted}`}>
          {filter === 'all'
            ? 'No supervisor tasks were returned for this owner.'
            : `No ${filter.replace(/-/g, ' ')} tasks found.`}
        </p>
      )}
      {filtered.map(task => {
        const label = task.state.replace(/_/g, ' ');
        return (
          <Row
            key={task.task_id}
            main={task.summary || `Task ${task.task_id.slice(0, 8)}`}
            sub={`${label} · ${formatDate(task.idempotent_replay ? '1970-01-01T00:00:00Z' : new Date().toISOString())}`}
            right={
              <div className="flex flex-wrap gap-2">
                <Chip>{label}</Chip>
                <button
                  type="button"
                  className={outline}
                  onClick={() => onSelect(task.task_id)}
                  aria-label={`Open details for ${task.summary || task.task_id}`}
                >
                  Open
                </button>
              </div>
            }
          />
        );
      })}
    </Panel>
  );
}

function NewTaskForm({
  api,
  devices,
  connected,
  onSuccess,
  onClose,
}: {
  api: WorkspaceApi;
  devices: { device_id: string; device_name: string }[];
  connected: boolean;
  onSuccess: (taskId: string) => void;
  onClose: () => void;
}) {
  const cache = useQueryClient();
  const [error, setError] = useState<string>();
  const submit = useMutation({
    mutationFn: (input: TaskInput) => api.submitTask(input),
    onSuccess: data => {
      onSuccess(data.task_id);
      void cache.invalidateQueries({ queryKey: tasksKey });
      void cache.invalidateQueries({ queryKey: approvalsKey });
    },
    onError: err => {
      setError(controlError(err, 'submit the task'));
    },
  });

  function handleForm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!connected) return;
    const data = new FormData(event.currentTarget);
    const objective = String(data.get('objective')).trim();
    const deviceId = String(data.get('device_id'));
    if (!objective) {
      const input = event.currentTarget.elements.namedItem('objective');
      if (input instanceof HTMLInputElement) {
        input.setCustomValidity('Enter a goal.');
        input.reportValidity();
      }
      return;
    }
    const capabilityRaw = data.getAll('capability') as string[];
    const capabilityScope: TaskInput['capability_scope'] = capabilityRaw.length
      ? (capabilityRaw as TaskInput['capability_scope'])
      : ['read_only'];
    submit.mutate({
      objective,
      capability_scope: capabilityScope,
      idempotency_key: crypto.randomUUID(),
      requester_device_id: deviceId,
    });
  }

  return (
    <Modal title="Submit a supervisor task" close={onClose} locked={submit.isPending} cls="max-w-[620px]">
      <form onSubmit={handleForm} className="space-y-4">
        <label className="block text-xs">
          Goal
          <textarea
            name="objective"
            className={`${field} mt-2`}
            rows={5}
            maxLength={2000}
            placeholder="Build me a portfolio website, test it, fix any issues and give me a working preview."
            required
          />
        </label>
        <label className="block text-xs">
          Target device (required for execution)
          <select name="device_id" className={field + ' mt-2'} required>
            <option value="" disabled>
              Choose a paired device
            </option>
            {devices.map(d => (
              <option key={d.device_id} value={d.device_id}>
                {d.device_name} · {d.device_id.slice(0, 8)}
              </option>
            ))}
          </select>
        </label>
        <fieldset className="text-xs">
          <legend className="font-semibold">Authorized capability scope</legend>
          <div className="mt-2 space-y-1">
            {['read_only', 'mac_read', 'assistant', 'development'].map(cap => (
              <label key={cap} className="flex items-center gap-2">
                <input type="checkbox" name="capability" value={cap} defaultChecked={cap === 'read_only'} />
                <span className={muted}>{cap.replace(/_/g, ' ')}</span>
              </label>
            ))}
          </div>
        </fieldset>
        {error && <Notice error>{error}</Notice>}
        <div className="flex flex-wrap gap-2 pt-2">
          <button type="button" className={outline} onClick={onClose} disabled={submit.isPending}>
            Cancel
          </button>
          <button type="submit" className={solid} disabled={!connected || submit.isPending}>
            {submit.isPending ? 'Submitting…' : 'Submit task'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function TaskTimeline({
  task,
  events,
  mode,
}: {
  task?: SupervisorTask;
  events: SupervisorEvent[];
  mode: string;
}) {
  const evidence = events.filter(item =>
    /tool|approval|operation|artifact|result|error/i.test(item.event_type)
  );

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold">Live execution timeline</h4>
        <Chip>{mode}</Chip>
      </div>
      {task && (
        <div className="rounded-lg border border-white/10 bg-white/[.03] p-3 text-xs">
          <div className="flex flex-wrap justify-between gap-2">
            <div>
              <b>Current state</b>
              <p className={muted}>{task.state.replace(/_/g, ' ')}</p>
            </div>
            <div>
              <b>Summary</b>
              <p className={muted}>{task.summary || 'No summary provided.'}</p>
            </div>
          </div>
        </div>
      )}
      {evidence.length > 0 && (
        <div>
          <h5 className="text-xs font-semibold">Tool and authorization evidence</h5>
          <ul className="mt-2 space-y-2">
            {evidence.slice(-15).map(item => (
              <li
                key={item.seq}
                className="rounded-lg border border-white/10 bg-white/[.03] p-3 text-xs"
              >
                <b>{item.event_type}</b>
                <p className={`mt-1 ${muted}`}>
                  {[
                    item.payload.to,
                    item.payload.step,
                    item.payload.operation,
                    item.payload.target,
                    item.payload.reason,
                    item.payload.error_code,
                  ]
                    .filter((value): value is string => typeof value === 'string' && Boolean(value))
                    .join(' · ')}
                </p>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div>
        <h5 className="text-xs font-semibold">All events</h5>
        {events.length === 0 && <p className={`mt-2 ${muted}`}>No events loaded yet.</p>}
        {events.length > 0 && (
          <ol className="mt-2 max-h-[420px] overflow-auto rounded-xl border border-white/10 px-3">
            {events.map(item => (
              <li key={item.seq} className="border-b border-white/5 py-2 last:border-b-0">
                <div className="flex flex-wrap items-start justify-between gap-2 text-xs">
                  <b>{item.event_type}</b>
                  <span className={muted}>{formatDate(item.created_at)}</span>
                </div>
                <p className={`mt-1 ${muted}`}>
                  {[
                    item.payload.to,
                    item.payload.step,
                    item.payload.operation,
                    item.payload.target,
                    item.payload.reason,
                  ]
                    .filter((value): value is string => typeof value === 'string' && Boolean(value))
                    .join(' · ')}
                </p>
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}

function TaskDetailPanel({
  api,
  taskId,
  connected,
  onClose,
}: {
  api: WorkspaceApi;
  taskId: string;
  connected: boolean;
  onClose: () => void;
}) {
  const { task, timeline, mode, error, retry } = useTaskProgress(api, taskId, connected);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [showResult, setShowResult] = useState(false);
  const cache = useQueryClient();

  const cancel = useMutation({
    mutationFn: () => api.cancelTask(taskId),
    onSuccess: () => {
      setConfirmCancel(false);
      void cache.invalidateQueries({ queryKey: tasksKey });
      void cache.invalidateQueries({ queryKey: approvalsKey });
      retry();
    },
  });

  const terminal = task && isTerminalTask(task.state);

  return (
    <Panel cls="p-5">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-semibold">Task detail</h2>
        <div className="flex flex-wrap gap-2">
          <Chip>{taskId.slice(0, 12)}…</Chip>
          <button type="button" className={outline} onClick={onClose}>
            Close
          </button>
        </div>
      </div>
      {error !== undefined && (
        <Notice error retry={connected ? retry : undefined}>
          {(() => {
            const err: Error = error instanceof Error ? error : new Error(String(error));
            return controlError(err, 'read task progress');
          })()}
        </Notice>
      )}
      {task?.state === 'WAITING_APPROVAL' && (
        <Notice>
          This task is awaiting approval. Review the pending request in{' '}
          <a className="underline" href="#/approvals">
            Approvals
          </a>
          .
        </Notice>
      )}
      {task?.state === 'FAILED' && (
        <Notice error>
          Supervisor task failed{task.error_code ? `: ${task.error_code}` : '.'}
        </Notice>
      )}
      {task?.state === 'COMPLETED' && task.result_text != null && (
        <>
          <button
            type="button"
            className={outline}
            onClick={() => setShowResult(open => !open)}
            aria-expanded={showResult}
          >
            {showResult ? 'Hide result' : 'Show result'}
          </button>
          {showResult && (
            <div className="mt-3 max-h-[420px] overflow-auto rounded-xl border border-white/10 p-3">
              <MarkdownMessage content={task.result_text} />
            </div>
          )}
        </>
      )}
      {task?.state === 'COMPLETED' && task.result_text == null && (
        <Notice>No result text was returned by the supervisor.</Notice>
      )}
      {!terminal && (
        <button
          type="button"
          className={`${outline} mt-3`}
          disabled={!connected || cancel.isPending}
          onClick={() => setConfirmCancel(true)}
        >
          Cancel task
        </button>
      )}
      <div className="mt-5">
        <TaskTimeline task={task} events={timeline.events} mode={mode} />
      </div>
      {confirmCancel && (
        <Modal
          title="Cancel supervisor task?"
          close={() => setConfirmCancel(false)}
          locked={cancel.isPending}
        >
          <p className={muted}>
            This stops the remaining task. An operation already dispatched may not be reversible.
          </p>
          {cancel.isError && (
            <Notice error>
              {controlError(
                cancel.error instanceof Error ? cancel.error : new Error(String(cancel.error)),
                'cancel the task'
              )}
            </Notice>
          )}
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
    </Panel>
  );
}

export function AutonomousWorkspace({
  api,
  devices,
  connected,
}: {
  api: WorkspaceApi;
  devices: { device_id: string; device_name: string }[];
  connected: boolean;
}) {
  const [selectedTask, setSelectedTask] = useState<string>();
  const [showNewTask, setShowNewTask] = useState(false);
  const [filter, setFilter] = useState<TaskFilter>('all');

  function onTaskCreated(taskId: string) {
    setSelectedTask(taskId);
    setShowNewTask(false);
  }

  return (
    <>
      <Head
        label="AUTONOMOUS WORKSPACE"
        title="Assign a goal. Watch it execute."
        desc="Submit natural-language tasks, follow the live timeline, and review verified results."
        action={
          <button
            type="button"
            className={solid}
            onClick={() => setShowNewTask(true)}
            disabled={!connected}
          >
            ＋ New task
          </button>
        }
      />
      <div className="grid min-w-0 gap-4 lg:grid-cols-[320px_1fr]">
        <div className="space-y-4">
          <TaskList
            api={api}
            connected={connected}
            onSelect={setSelectedTask}
            filter={filter}
            setFilter={setFilter}
          />
          {!connected && (
            <Unavailable
              title="Backend connection unverified"
              reason="Task submission and monitoring require a healthy supervisor worker on your Mac host."
            />
          )}
        </div>
        <div className="min-w-0">
          {selectedTask ? (
            <TaskDetailPanel
              api={api}
              taskId={selectedTask}
              connected={connected}
              onClose={() => setSelectedTask(undefined)}
            />
          ) : (
            <Panel cls="p-5">
              <h2 className="font-semibold">Task overview</h2>
              <p className={`mt-2 ${muted}`}>
                Select a task from the list to follow its live execution timeline, or submit a new
                goal.
              </p>
              <div className="mt-4 space-y-2 text-xs">
                <Line
                  title="Natural-language goals"
                  desc="Describe what you want. The backend generates a plan and executes it."
                />
                <Line
                  title="Live timeline"
                  desc="Events stream from the supervisor worker with replay on reconnect."
                />
                <Line
                  title="Approvals"
                  desc="Sensitive operations require explicit owner approval with expiry."
                />
                <Line
                  title="Verified artifacts"
                  desc="Completed development tasks can publish ZIP archives with SHA-256 integrity."
                />
              </div>
            </Panel>
          )}
        </div>
      </div>
      {showNewTask && (
        <NewTaskForm
          api={api}
          devices={devices}
          connected={connected}
          onSuccess={onTaskCreated}
          onClose={() => setShowNewTask(false)}
        />
      )}
    </>
  );
}
