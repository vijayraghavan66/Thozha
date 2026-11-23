import { useState, type FormEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { mayHaveBeenAccepted, type WorkspaceApi } from '../api/client';
import { type PairedDevice, type TaskInput, type TaskScope } from '../api/control';
import { field, muted, outline, solid } from '../components/styles';
import { Chip, Head, Loading, Notice, Panel, Row, Stat } from '../components/ui';
import { Approvals } from './Approvals';
import { controlError, taskHref, tasksKey, useDevices, useTasks } from './controlState';
import { DevicePicker } from './DevicePicker';
import { TaskDetail } from './TaskDetail';

function TaskForm({
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
  const submit = useMutation({
    mutationFn: (body: TaskInput) => api.submitTask(body),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: tasksKey });
    },
  });
  const deviceId = devices.some(item => item.device_id === selection)
    ? selection
    : devices.length === 1
      ? devices[0].device_id
      : '';
  const unavailable =
    submit.isError &&
    submit.error instanceof Error &&
    controlError(submit.error, 'submit') === 'Supervisor task worker is unavailable';
  const uncertain = submit.isError && !unavailable && mayHaveBeenAccepted(submit.error);

  function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!connected || !deviceId || !objective.trim() || submit.isPending) return;
    const accepted = {
      onSuccess: (data: Awaited<ReturnType<WorkspaceApi['submitTask']>>) =>
        onAccepted(data.task_id),
    };
    if (uncertain) {
      if (submit.variables?.requester_device_id === deviceId)
        submit.mutate(submit.variables, accepted);
      return;
    }
    submit.mutate(
      {
        objective: objective.trim(),
        requester_device_id: deviceId,
        capability_scope: [scope],
        idempotency_key: crypto.randomUUID(),
      },
      accepted
    );
  }

  return (
    <Panel cls="th-enter mb-5 max-w-[780px] p-6">
      <h2 className="font-semibold">Submit a supervisor task</h2>
      <p className={`mt-2 ${muted}`}>
        This is separate from chat. The host decides which capabilities and exact operations are
        authorized. Submission does not grant an approval.
      </p>
      <form className="mt-4 space-y-4" onSubmit={send}>
        <fieldset
          className="min-w-0 space-y-4"
          disabled={!connected || submit.isPending || uncertain}
        >
          <label className="block text-xs">
            Objective
            <textarea
              required
              maxLength={20_000}
              rows={3}
              className={field + ' mt-2'}
              value={objective}
              onChange={event => setObjective(event.target.value)}
            />
          </label>
          <div className="grid grid-cols-2 gap-4 max-md:grid-cols-1">
            <DevicePicker devices={devices} value={deviceId} change={setSelection} />
            <label className="block text-xs">
              Capability scope
              <select
                className={field + ' mt-2'}
                value={scope}
                onChange={event => {
                  const next = event.target.value;
                  if (next === 'read_only' || next === 'assistant' || next === 'development')
                    setScope(next);
                }}
              >
                <option value="read_only">Read-only</option>
                <option value="assistant">Assistant (supervisor-gated)</option>
                <option value="development">Development (allowlisted)</option>
              </select>
            </label>
          </div>
        </fieldset>
        {devices.length === 0 && (
          <p className={muted}>
            An active mapped pairing is required. Check Device Hub for details.
          </p>
        )}
        {submit.isError && (
          <Notice error>
            {controlError(submit.error, 'submit the task')}
            {uncertain &&
              ' Acceptance is unconfirmed. Retry with the original key, or check task history before starting a new request.'}
          </Notice>
        )}
        <button
          type="submit"
          className={solid}
          disabled={
            !connected ||
            !deviceId ||
            !objective.trim() ||
            submit.isPending ||
            (uncertain && submit.variables?.requester_device_id !== deviceId)
          }
        >
          {submit.isPending ? 'Submitting…' : uncertain ? 'Retry same task' : 'Submit task'}
        </button>
      </form>
    </Panel>
  );
}

export default function DeveloperStudio({
  api,
  connected,
  taskId,
  draft,
  clearDraft,
}: {
  api: WorkspaceApi;
  connected: boolean;
  taskId?: string;
  draft?: string;
  clearDraft: () => void;
}) {
  const [builder, setBuilder] = useState(false);
  const [filter, setFilter] = useState<'active' | 'completed' | 'all'>('all');
  const devices = useDevices(api, connected);
  const tasks = useTasks(api, filter, connected);
  const mapped = devices.isSuccess ? devices.data.devices : [];
  const showForm = builder || draft !== undefined;
  return (
    <>
      <Head
        label="EXECUTION / EVIDENCE"
        title="Developer Studio"
        desc="Submit and inspect supervisor tasks, review exact-operation approvals, and retrieve authorized evidence."
        action={
          <button
            type="button"
            className={solid}
            aria-expanded={showForm}
            disabled={!showForm && (!connected || mapped.length === 0)}
            onClick={() => {
              if (showForm) clearDraft();
              setBuilder(!showForm);
            }}
          >
            {showForm ? 'Close task form' : 'New supervisor task'}
          </button>
        }
      />
      {showForm && (
        <TaskForm
          api={api}
          devices={mapped}
          connected={connected}
          prefill={draft ?? ''}
          onAccepted={id => {
            clearDraft();
            setBuilder(false);
            window.location.hash = taskHref(id);
          }}
        />
      )}
      <div className="grid grid-cols-3 gap-3 max-md:grid-cols-1">
        <Stat label="REPOSITORY" value="Thozha" detail="Workspace name; no GitHub connection" />
        <Stat label="CI" value="—" detail="Build and test results not connected" />
        <Stat
          label="SUPERVISOR TASKS"
          value={tasks.isSuccess ? String(tasks.data.tasks.length) : '—'}
          detail={
            tasks.isSuccess
              ? `Tasks in this view · up to ${filter === 'active' ? 100 : 50}`
              : 'Requires Web supervisor access'
          }
        />
      </div>
      <div className="mt-5 grid grid-cols-2 items-start gap-4 max-lg:grid-cols-1">
        <Panel cls="min-w-0 p-5">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <h2 className="font-semibold">Task timeline</h2>
            <button
              type="button"
              className={outline}
              disabled={!connected || tasks.isFetching}
              onClick={() => void tasks.refetch()}
            >
              Refresh tasks
            </button>
          </div>
          {taskId && <TaskDetail key={taskId} api={api} taskId={taskId} connected={connected} />}
          <label className="mt-3 block text-xs">
            Task history filter
            <select
              className={field + ' mt-2'}
              value={filter}
              onChange={event => {
                const next = event.target.value;
                if (next === 'active' || next === 'completed' || next === 'all') setFilter(next);
              }}
            >
              <option value="all">All recent tasks</option>
              <option value="active">Active</option>
              <option value="completed">Completed, failed or cancelled</option>
            </select>
          </label>
          {tasks.isPending && connected && <Loading label="Loading persisted tasks…" />}
          {tasks.isError && (
            <div className="mt-3">
              <Notice error>{controlError(tasks.error, 'list supervisor tasks')}</Notice>
            </div>
          )}
          {tasks.isSuccess && !tasks.data.tasks.length && (
            <p className={`mt-3 ${muted}`}>No saved tasks in this view.</p>
          )}
          {tasks.isSuccess &&
            tasks.data.tasks.map(item => (
              <Row
                key={item.task_id}
                main={item.summary || `Task ${item.task_id.slice(0, 8)}`}
                sub={item.task_id}
                right={
                  <div className="flex flex-wrap items-center gap-2">
                    <Chip>{item.state.replace(/_/g, ' ')}</Chip>
                    <a
                      href={taskHref(item.task_id)}
                      className={`${outline} inline-block`}
                      aria-current={item.task_id === taskId ? 'true' : undefined}
                    >
                      View task {item.task_id.slice(0, 8)}
                    </a>
                  </div>
                }
              />
            ))}
          {mapped.length === 0 && (
            <p className={`mt-3 ${muted}`}>
              Task submission requires an active paired device explicitly mapped to this Web owner.
            </p>
          )}
          <button type="button" className={`${outline} mt-3`} disabled>
            Run project checks
          </button>
        </Panel>
        <Approvals api={api} devices={mapped} connected={connected} />
      </div>
    </>
  );
}
