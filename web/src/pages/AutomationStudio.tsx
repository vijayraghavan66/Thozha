import { useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { mayHaveBeenAccepted, type WorkspaceApi } from '../api/client';
import type {
  AutomationAction,
  AutomationCapabilities,
  AutomationInput,
  AutomationPatch,
  PairedDevice,
  WebAutomation,
} from '../api/control';
import { field, formatDate, muted, outline, solid } from '../components/styles';
import { Chip, Head, Loading, Modal, Notice, Panel, Row } from '../components/ui';
import { AutomationDrafts } from './WorkspacePages';
import { DevicePicker } from './DevicePicker';
import {
  automationCapabilitiesKey,
  automationsKey,
  controlError,
  httpStatus,
  taskHref,
  tasksKey,
  useDevices,
} from './controlState';

const automationDetailKey = (id: string) => [...automationsKey, id] as const;
const scheduleLabels = { interval: 'Interval', once: 'One time', cron: 'Cron' };
type ScheduleSave =
  | { kind: 'create'; body: AutomationInput; key: string }
  | { kind: 'update'; id: string; body: AutomationPatch };

function AutomationForm({
  api,
  connected,
  current,
  devices,
  done,
}: {
  api: WorkspaceApi;
  connected: boolean;
  current?: WebAutomation;
  devices: PairedDevice[];
  done: (id: string) => void;
}) {
  const cache = useQueryClient();
  const [type, setType] = useState<AutomationInput['schedule_type']>(
    current?.schedule_type ?? 'interval'
  );
  const [value, setValue] = useState(current?.schedule_value ?? '300');
  const [selection, setSelection] = useState('');
  const [problem, setProblem] = useState('');
  const deviceId = devices.some(device => device.device_id === selection)
    ? selection
    : devices.length === 1
      ? devices[0].device_id
      : '';
  const save = useMutation({
    mutationFn: async (request: ScheduleSave) => {
      if (request.kind === 'update') {
        await api.updateAutomation(request.id, request.body);
        return request.id;
      }
      const created = await api.createAutomation(request.body, request.key);
      return created.automation.id;
    },
    onSettled: () => {
      void cache.invalidateQueries({ queryKey: automationsKey });
    },
  });
  const uncertain = !current && save.isError && mayHaveBeenAccepted(save.error);
  const retry =
    save.variables?.kind === 'create' && save.variables.body.device_id === deviceId
      ? save.variables
      : undefined;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!connected || save.isPending || (!current && !deviceId)) return;
    const accepted = { onSuccess: done };
    if (uncertain) {
      if (retry) save.mutate(retry, accepted);
      return;
    }
    const fields = new FormData(event.currentTarget);
    const prompt = String(fields.get('prompt') ?? '').trim();
    const scheduleValue = value.trim();
    const timezone = String(fields.get('timezone') ?? '').trim();
    if (!prompt || !scheduleValue) {
      setProblem('Enter a read-only objective and schedule value.');
      return;
    }
    if (
      type === 'once' &&
      (!/(Z|[+-]\d{2}:\d{2})$/i.test(scheduleValue) || !Number.isFinite(Date.parse(scheduleValue)))
    ) {
      setProblem('Enter an ISO-8601 date and time with a timezone offset, or Z for UTC.');
      return;
    }
    setProblem('');
    if (current) {
      const patch: AutomationPatch = {
        ...(prompt !== current.prompt ? { prompt } : {}),
        ...(type !== current.schedule_type ? { schedule_type: type } : {}),
        ...(scheduleValue !== current.schedule_value ? { schedule_value: scheduleValue } : {}),
        ...(timezone && timezone !== current.timezone ? { timezone } : {}),
      };
      if (!Object.keys(patch).length) {
        setProblem('No schedule changes to save.');
        return;
      }
      save.mutate({ kind: 'update', id: current.id, body: patch }, accepted);
      return;
    }
    save.mutate(
      {
        kind: 'create',
        body: {
          prompt,
          schedule_type: type,
          schedule_value: scheduleValue,
          ...(timezone ? { timezone } : {}),
          device_id: deviceId,
        },
        key: crypto.randomUUID(),
      },
      accepted
    );
  }
  return (
    <Panel cls="th-enter mt-5 max-w-[780px] p-6">
      <h2 className="font-semibold">
        {current ? 'Edit read-only schedule' : 'Create a read-only schedule'}
      </h2>
      <p className={`mt-2 ${muted}`}>
        The backend submits each run as a read-only supervisor task. This form cannot schedule
        arbitrary tools, write actions or client-defined approvals.
      </p>
      <form onSubmit={submit} className="mt-5 space-y-4">
        <fieldset
          className="min-w-0 space-y-4"
          disabled={!connected || save.isPending || uncertain}
        >
          <label className="block text-xs">
            Read-only objective
            <textarea
              name="prompt"
              className={field + ' mt-2'}
              defaultValue={current?.prompt}
              required
              maxLength={2000}
              rows={3}
            />
          </label>
          {!current && <DevicePicker devices={devices} value={deviceId} change={setSelection} />}
          <div className="grid grid-cols-2 gap-4 max-md:grid-cols-1">
            <label className="text-xs">
              Schedule type
              <select
                className={field + ' mt-2'}
                value={type}
                onChange={event => {
                  const next = event.target.value;
                  if (next === 'interval' || next === 'once' || next === 'cron') {
                    setType(next);
                    setValue(next === 'interval' ? '300' : '');
                  }
                }}
              >
                <option value="interval">Interval</option>
                <option value="once">One time</option>
                <option value="cron">Cron</option>
              </select>
            </label>
            <label className="text-xs">
              {type === 'interval'
                ? 'Interval (seconds)'
                : type === 'once'
                  ? 'Run at (ISO-8601 with offset)'
                  : 'Cron expression'}
              <input
                className={field + ' mt-2'}
                type={type === 'interval' ? 'number' : 'text'}
                step={type === 'interval' ? 'any' : undefined}
                required
                min={type === 'interval' ? 300 : undefined}
                max={type === 'interval' ? 31_536_000 : undefined}
                maxLength={200}
                placeholder={
                  type === 'once'
                    ? 'YYYY-MM-DDTHH:MM:SS+05:30'
                    : type === 'cron'
                      ? '0 9 * * 1-5'
                      : undefined
                }
                value={value}
                onChange={event => setValue(event.target.value)}
              />
            </label>
          </div>
          <label className="block text-xs">
            Timezone (IANA, optional)
            <input
              name="timezone"
              className={field + ' mt-2'}
              placeholder="e.g. Asia/Kolkata"
              defaultValue={current?.timezone ?? ''}
              maxLength={80}
            />
          </label>
          {current?.timezone && (
            <p className={muted}>Leave timezone blank to keep the current timezone.</p>
          )}
        </fieldset>
        {problem && <Notice error>{problem}</Notice>}
        {save.isError && (
          <Notice error>
            {controlError(save.error, 'save the schedule')}
            {uncertain && ' Acceptance is unconfirmed. A retry uses the original schedule and key.'}
          </Notice>
        )}
        <button
          type="submit"
          className={solid}
          disabled={
            !connected || save.isPending || (!current && !deviceId) || (uncertain && !retry)
          }
        >
          {save.isPending
            ? 'Saving…'
            : uncertain
              ? 'Retry same schedule'
              : current
                ? 'Save changes'
                : 'Create schedule'}
        </button>
      </form>
    </Panel>
  );
}

const actionLabels = {
  pause: 'Pause',
  enable: 'Resume',
  delete: 'Delete schedule',
  manual_trigger: 'Run now',
};
const operationReason = (caps: AutomationCapabilities, name: string) =>
  caps.unavailable_reasons[name] || 'This operation is not available on the connected backend.';

function ScheduleDetail({
  api,
  id,
  connected,
  caps,
  edit,
}: {
  api: WorkspaceApi;
  id: string;
  connected: boolean;
  caps: AutomationCapabilities;
  edit: (item: WebAutomation) => void;
}) {
  const cache = useQueryClient();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const detail = useQuery({
    queryKey: automationDetailKey(id),
    queryFn: ({ signal }) => api.getAutomation(id, signal),
    enabled: connected && caps.operations.get === true,
  });
  const history = useQuery({
    queryKey: [...automationDetailKey(id), 'history'],
    queryFn: ({ signal }) => api.automationHistory(id, signal),
    enabled: connected && caps.operations.history === true,
  });
  const action = useMutation({
    mutationFn: (input: { kind: AutomationAction; key?: string }) =>
      api.actOnAutomation(id, input.kind, input.key),
    onSuccess: () => setConfirmDelete(false),
    onSettled: () => {
      void cache.invalidateQueries({ queryKey: automationsKey });
      void cache.invalidateQueries({ queryKey: tasksKey });
    },
  });
  const schedule = detail.isSuccess ? detail.data.automation : undefined;
  const uncertainTrigger =
    action.isError &&
    action.variables?.kind === 'manual_trigger' &&
    mayHaveBeenAccepted(action.error);
  function run(kind: AutomationAction) {
    if (
      !connected ||
      action.isPending ||
      caps.operations[kind] !== true ||
      !schedule ||
      schedule.status === 'cancelled'
    )
      return;
    if (uncertainTrigger) {
      if (kind === 'manual_trigger' && action.variables) action.mutate(action.variables);
      return;
    }
    action.mutate({ kind, key: kind === 'manual_trigger' ? crypto.randomUUID() : undefined });
  }

  function actionButton(kind: AutomationAction) {
    const allowed = caps.operations[kind] === true;
    return (
      <button
        type="button"
        className={outline}
        disabled={
          !connected ||
          action.isPending ||
          !allowed ||
          (uncertainTrigger && kind !== 'manual_trigger')
        }
        title={allowed ? undefined : operationReason(caps, kind)}
        onClick={() => (kind === 'delete' ? setConfirmDelete(true) : run(kind))}
      >
        {kind === 'manual_trigger' && uncertainTrigger
          ? 'Retry same manual run'
          : actionLabels[kind]}
      </button>
    );
  }
  return (
    <Panel cls="th-enter mt-5 min-w-0 space-y-3 p-6">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h2 className="font-semibold">Schedule details</h2>
        <button
          type="button"
          className={outline}
          disabled={!connected || detail.isFetching || history.isFetching}
          onClick={() => void cache.invalidateQueries({ queryKey: automationDetailKey(id) })}
        >
          Refresh schedule and history
        </button>
      </div>
      {detail.isPending && connected && caps.operations.get && (
        <Loading label="Loading the saved schedule…" />
      )}
      {detail.isError && <Notice error>{controlError(detail.error, 'read the schedule')}</Notice>}
      {schedule && (
        <>
          <Row
            main={schedule.prompt}
            sub={`${scheduleLabels[schedule.schedule_type]} · ${schedule.schedule_value}${schedule.timezone ? ` · ${schedule.timezone}` : ''}`}
            right={<Chip>{schedule.status.toUpperCase()}</Chip>}
          />
          <p className={muted}>
            {schedule.next_run
              ? `Next run: ${formatDate(schedule.next_run)}`
              : 'No next run scheduled.'}
          </p>
          {schedule.last_supervisor_task_id && (
            <a
              className={`${outline} inline-block`}
              href={taskHref(schedule.last_supervisor_task_id)}
            >
              View latest scheduled task
            </a>
          )}
          {schedule.status !== 'cancelled' && (
            <div className="flex flex-wrap gap-2">
              {(schedule.status === 'active' || schedule.status === 'paused') && (
                <>
                  <button
                    type="button"
                    className={outline}
                    disabled={
                      !connected || !caps.operations.update || action.isPending || uncertainTrigger
                    }
                    title={caps.operations.update ? undefined : operationReason(caps, 'update')}
                    onClick={() => edit(schedule)}
                  >
                    Edit schedule
                  </button>
                  {actionButton(schedule.status === 'active' ? 'pause' : 'enable')}
                </>
              )}
              {actionButton('manual_trigger')}
              {actionButton('delete')}
            </div>
          )}
        </>
      )}
      {action.isError && !confirmDelete && (
        <Notice error>
          {controlError(action.error, 'change the schedule')}
          {uncertainTrigger &&
            ' The manual run may have been accepted. Check history or retry it using the original key.'}
        </Notice>
      )}
      {action.data && (
        <Notice>
          Server response: {action.data.state || action.data.status}.
          {action.data.supervisor_task_id && (
            <>
              {' '}
              <a className="underline" href={taskHref(action.data.supervisor_task_id)}>
                Follow supervisor task
              </a>
            </>
          )}
        </Notice>
      )}
      <h3 className="pt-3 text-sm font-semibold">Run history</h3>
      {!caps.operations.history && <p className={muted}>{operationReason(caps, 'history')}</p>}
      {history.isPending && connected && caps.operations.history && (
        <Loading label="Loading persisted run history…" />
      )}
      {history.isError && <Notice error>{controlError(history.error, 'read run history')}</Notice>}
      {history.isSuccess && !history.data.items.length && (
        <p className={muted}>No runs have been recorded.</p>
      )}
      {history.isSuccess &&
        history.data.items.map((run, index) => {
          const taskState =
            typeof run.supervisor_task?.state === 'string' ? run.supervisor_task.state : null;
          return (
            <Row
              key={`${run.started_at}-${run.supervisor_task_id ?? index}`}
              main={formatDate(run.started_at)}
              sub={[run.state, taskState ? `Supervisor: ${taskState}` : '', run.error || run.result]
                .filter(Boolean)
                .join(' · ')}
              right={
                run.supervisor_task_id && (
                  <a className={`${outline} inline-block`} href={taskHref(run.supervisor_task_id)}>
                    View supervisor task
                  </a>
                )
              }
            />
          );
        })}
      {confirmDelete && (
        <Modal
          title="Delete this schedule?"
          close={() => setConfirmDelete(false)}
          locked={action.isPending}
        >
          <p className={muted}>
            Future occurrences will be cancelled. Run history is retained; tasks already submitted
            are not cancelled by deleting the schedule.
          </p>
          {action.isError && (
            <Notice error>{controlError(action.error, 'delete the schedule')}</Notice>
          )}
          <div className="mt-4 flex flex-wrap gap-2">
            <button
              type="button"
              data-dialog-focus
              className={outline}
              disabled={action.isPending}
              onClick={() => setConfirmDelete(false)}
            >
              Keep schedule
            </button>
            <button
              type="button"
              className={solid}
              disabled={
                !connected ||
                action.isPending ||
                !caps.operations.delete ||
                !schedule ||
                schedule.status === 'cancelled'
              }
              onClick={() => run('delete')}
            >
              Confirm deletion
            </button>
          </div>
        </Modal>
      )}
    </Panel>
  );
}

export default function AutomationStudio({
  api,
  connected,
}: {
  api: WorkspaceApi;
  connected: boolean;
}) {
  const [builder, setBuilder] = useState<'create' | WebAutomation>();
  const [selected, select] = useState('');
  const capabilities = useQuery({
    queryKey: automationCapabilitiesKey,
    queryFn: ({ signal }) => api.automationCapabilities(signal),
    enabled: connected,
  });
  const caps = capabilities.isSuccess ? capabilities.data : undefined;
  const devices = useDevices(api, connected && caps?.operations.create === true);
  const list = useQuery({
    queryKey: automationsKey,
    queryFn: ({ signal }) => api.listAutomations(signal),
    enabled: connected && caps?.operations.list === true,
  });
  const mapped = devices.isSuccess ? devices.data.devices : [];
  if (httpStatus(capabilities.error) === 404) return <AutomationDrafts />;
  const canEdit =
    builder === 'create'
      ? caps?.operations.create === true && mapped.length > 0
      : caps?.operations.update === true;
  return (
    <>
      <Head
        label="WORKFLOW DESIGN"
        title="Automations"
        desc="Schedule owner-scoped, read-only supervisor tasks."
        action={
          <button
            type="button"
            className={solid}
            aria-expanded={Boolean(builder)}
            disabled={
              !builder && (!connected || caps?.operations.create !== true || mapped.length === 0)
            }
            onClick={() => setBuilder(builder ? undefined : 'create')}
          >
            {builder ? 'Close builder' : '＋ New schedule'}
          </button>
        }
      />
      {capabilities.isPending && connected && <Loading label="Checking automation capabilities…" />}
      {capabilities.isError && (
        <Notice error retry={connected ? () => void capabilities.refetch() : undefined}>
          {controlError(capabilities.error, 'check automation capabilities')}
        </Notice>
      )}
      {caps && !caps.operations.create && (
        <Notice>Schedule creation is unavailable: {operationReason(caps, 'create')}</Notice>
      )}
      {caps && !caps.operations.manual_trigger && caps.operations.create && (
        <Notice>Manual runs are unavailable: {operationReason(caps, 'manual_trigger')}</Notice>
      )}
      {caps?.operations.create && devices.isError && (
        <Notice error>{controlError(devices.error, 'list automation requester devices')}</Notice>
      )}
      {builder && canEdit && (
        <AutomationForm
          key={builder === 'create' ? builder : builder.id}
          api={api}
          connected={connected}
          devices={mapped}
          current={builder === 'create' ? undefined : builder}
          done={id => {
            setBuilder(undefined);
            select(id);
          }}
        />
      )}
      {caps?.operations.list && (
        <Panel cls="mt-5 p-6">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <h2 className="font-semibold">Your schedules</h2>
            <button
              type="button"
              className={outline}
              disabled={!connected || list.isFetching || capabilities.isFetching}
              onClick={() => {
                void capabilities.refetch();
                void list.refetch();
              }}
            >
              Refresh schedules
            </button>
          </div>
          {list.isPending && connected && <Loading label="Loading owner-scoped schedules…" />}
          {list.isError && <Notice error>{controlError(list.error, 'list schedules')}</Notice>}
          {list.isSuccess && !list.data.items.length && (
            <p className={`py-10 text-center ${muted}`}>No Web schedules saved for this owner.</p>
          )}
          {list.isSuccess &&
            list.data.items.map(item => (
              <Row
                key={item.id}
                main={item.prompt}
                sub={
                  item.next_run
                    ? `Next run: ${formatDate(item.next_run)}`
                    : scheduleLabels[item.schedule_type]
                }
                right={
                  <div className="flex flex-wrap items-center gap-2">
                    <Chip>{item.status.toUpperCase()}</Chip>
                    <button
                      type="button"
                      className={outline}
                      aria-pressed={selected === item.id}
                      disabled={!connected || !caps.operations.get}
                      onClick={() => {
                        setBuilder(undefined);
                        select(item.id);
                      }}
                    >
                      View schedule
                    </button>
                  </div>
                }
              />
            ))}
        </Panel>
      )}
      {selected && caps && (
        <ScheduleDetail
          key={selected}
          api={api}
          id={selected}
          connected={connected}
          caps={caps}
          edit={setBuilder}
        />
      )}
    </>
  );
}
