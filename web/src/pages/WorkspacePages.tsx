import { useState, type FormEvent } from 'react';
import { Chip, Head, Line, Panel, Row, Stat, Unavailable } from '../components/ui';
import { LiquidCard } from '../components/LiquidSurface';
import { WaterSurface } from '../components/WaterSurface';
import { field, formatDate, muted, outline, solid } from '../components/styles';
import type { WorkspaceApi } from '../api/client';
import { httpStatus, taskHref, useApprovals, useDevices, useTasks } from './controlState';

export function Overview({
  api,
  onChat,
  onDevices,
  onSupervisor,
  onSettings,
  connected,
  reduced,
}: {
  api: WorkspaceApi;
  onChat: () => void;
  onDevices: () => void;
  onSupervisor: () => void;
  onSettings: () => void;
  connected: boolean;
  reduced: boolean;
}) {
  const tasks = useTasks(api, 'active', connected);
  const devices = useDevices(api, connected);
  const approvals = useApprovals(api, connected);
  const stats = (
    query: typeof tasks | typeof devices | typeof approvals,
    count: number | undefined,
    caption: string
  ) => ({
    value: query.isSuccess && count !== undefined ? String(count) : '—',
    detail: !connected
      ? 'Connection unverified'
      : query.isSuccess
        ? caption
        : [403, 404, 503].includes(httpStatus(query.error) ?? 0)
          ? 'Unavailable for this owner'
          : query.isError
            ? 'Could not load from the backend'
            : 'Checking Web API…',
  });
  return (
    <>
      <Head
        label="YOUR WORKSPACE"
        title="A quieter kind of control."
        desc="One place to think, build, automate, and keep everything in view."
        action={
          <button type="button" className={solid} onClick={onChat}>
            ＋ Start a conversation
          </button>
        }
      />
      <div className="relative mb-5 min-h-[255px] overflow-hidden rounded-[24px] border border-white/[.15] bg-[#202126] p-8 md:p-10">
        <WaterSurface reduced={reduced} />
        <div className="relative max-w-[490px]">
          <div className="text-[10px] font-semibold tracking-[.25em] text-[#b8b9c0]">
            THOZHA / 3.0
          </div>
          <h2 className="mt-5 text-3xl font-medium leading-[1.16] tracking-[-.055em] md:text-[42px]">
            Intelligence,
            <br />
            <span className="text-[#aeb0b9]">without the noise.</span>
          </h2>
          <p className="mt-4 max-w-[380px] text-xs leading-6 text-[#afb0b9]">
            One calm place for the work that matters. Your conversations stay on your Thozha host.
          </p>
          <button
            type="button"
            className="mt-5 text-xs font-semibold text-white hover:underline"
            onClick={onChat}
          >
            Enter the workspace ↗
          </button>
        </div>
      </div>
      <div className="grid grid-cols-4 gap-3 max-xl:grid-cols-2 max-[360px]:grid-cols-1">
        <Stat
          label="TASKS"
          {...stats(tasks, tasks.data?.tasks.length, 'Active tasks returned · up to 100')}
        />
        <Stat
          label="DEVICES"
          {...stats(devices, devices.data?.count, 'Active pairings mapped to you')}
        />
        <Stat
          label="APPROVALS"
          {...stats(approvals, approvals.data?.count, 'Pending approvals returned · up to 100')}
        />
        <Stat
          label="WEB SESSION"
          value={connected ? 'Active' : 'Unverified'}
          detail="Browser session only, not Mac health"
        />
      </div>
      <div className="mt-5 grid grid-cols-[1.25fr_1fr] gap-4 max-xl:grid-cols-1">
        <Panel cls="p-5">
          <div className="mb-2 flex justify-between gap-2">
            <h2 className="font-semibold">Activity</h2>
            <Chip>WEB v1</Chip>
          </div>
          <Line
            title="Conversation history"
            desc="Review server-saved messages and response events in the AI Workspace."
          />
          <Line
            title="Task activity"
            desc={
              tasks.isSuccess
                ? tasks.data.tasks.length
                  ? 'Recent active tasks saved by the supervisor.'
                  : 'No active tasks were returned.'
                : !connected
                  ? 'Task activity will be checked after reconnection.'
                  : 'See Developer Studio for task availability and approvals.'
            }
          />
          {connected &&
            tasks.isSuccess &&
            tasks.data.tasks.slice(0, 3).map(task => (
              <Row
                key={task.task_id}
                main={task.summary || `Task ${task.task_id.slice(0, 8)}`}
                sub={task.state.replace(/_/g, ' ')}
                right={
                  <a className={`${outline} inline-block`} href={taskHref(task.task_id)}>
                    Open task
                  </a>
                }
              />
            ))}
          <div className="mt-3 flex flex-wrap gap-2">
            <button type="button" className={outline} onClick={onChat}>
              Open conversations
            </button>
            <button type="button" className={outline} onClick={onSupervisor}>
              Open supervisor
            </button>
          </div>
        </Panel>
        <Panel cls="p-5">
          <div className="mb-2 flex justify-between gap-2">
            <h2 className="font-semibold">Your ecosystem</h2>
            <Chip>WEB v1</Chip>
          </div>
          <Line
            title="Workspace connection"
            desc="Only the browser session is verified. This is not a device health check."
          />
          <Line
            title="Devices and pairing"
            desc={
              devices.isSuccess
                ? 'Device Hub lists active mapped pairings, not live device presence.'
                : 'See Device Hub for the status of the browser-to-device mapping.'
            }
          />
          <div className="mt-3 flex flex-wrap gap-2">
            <button type="button" className={outline} onClick={onDevices}>
              Open Device Hub
            </button>
            <button type="button" className={outline} onClick={onSettings}>
              View session settings
            </button>
          </div>
        </Panel>
      </div>
    </>
  );
}

const deviceExamples = [
  ['Mac workstation', 'Host for the assistant and device services.'],
  ['Android companion', 'Uses its separate paired-device identity.'],
  ['Living room TV', 'An illustrative media-device category.'],
  ['Smart speaker', 'An illustrative audio-device category.'],
] as const;

export function DeviceExamples() {
  const [show, setShow] = useState(false);
  const [selected, select] = useState<(typeof deviceExamples)[number]>(deviceExamples[0]);
  return (
    <>
      <Head
        label="CONSENT-FIRST ECOSYSTEM"
        title="Device Hub"
        desc="Review what device access this browser workspace supports."
        action={
          <button
            type="button"
            className={solid}
            onClick={() => setShow(!show)}
            aria-expanded={show}
          >
            {show ? 'Hide device examples' : 'Explore sample devices'}
          </button>
        }
      />
      <Unavailable
        title="Bring your devices together."
        reason="This Web owner has no available device mapping. These examples are not paired devices. Web v1 never scans your network or enrolls devices."
      >
        <button type="button" className={outline} disabled>
          Discover devices
        </button>
      </Unavailable>
      {show && (
        <>
          <div className="mb-3 mt-5">
            <Chip>SAMPLE DEVICE TYPES · NOT DISCOVERED</Chip>
          </div>
          <div className="grid grid-cols-4 gap-3 max-xl:grid-cols-2 max-sm:grid-cols-1">
            {deviceExamples.map(example => (
              <LiquidCard
                key={example[0]}
                aria-pressed={selected === example}
                onClick={() => select(example)}
                className={`flex min-h-[145px] flex-col items-start rounded-2xl border p-5 text-left ${selected === example ? 'border-white/40 bg-[#292a30]' : 'border-white/10 bg-[#17181b]'}`}
              >
                <span aria-hidden="true" className="text-3xl">
                  ⌘
                </span>
                <b className="mt-5 text-sm">{example[0]}</b>
                <p className="mt-2 text-xs text-[#a4a5af]">Sample only</p>
              </LiquidCard>
            ))}
          </div>
          <Panel key={selected[0]} cls="th-enter mt-5 p-6">
            <h2 className="font-semibold">{selected[0]}</h2>
            <p className={`mt-3 ${muted}`}>{selected[1]} No device was contacted.</p>
            <div className="mt-5 flex flex-wrap gap-3">
              <button type="button" className={outline} disabled>
                Pair device
              </button>
              <button type="button" className={outline} disabled>
                Manage permissions
              </button>
              <button type="button" className={outline} disabled>
                Revoke device
              </button>
            </div>
          </Panel>
        </>
      )}
    </>
  );
}

type Draft = {
  id: string;
  name: string;
  trigger: string;
  condition: string;
  action: string;
  approval: boolean;
};
export function AutomationDrafts() {
  const [builder, setBuilder] = useState(false);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  function saveDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const name = String(data.get('name')).trim();
    if (!name) {
      const input = event.currentTarget.elements.namedItem('name');
      if (input instanceof HTMLInputElement) {
        input.setCustomValidity('Enter a routine name.');
        input.reportValidity();
      }
      return;
    }
    setDrafts(items => [
      ...items,
      {
        id: crypto.randomUUID(),
        name,
        trigger: String(data.get('trigger')),
        condition: String(data.get('condition')).trim(),
        action: String(data.get('action')),
        approval: data.has('approval'),
      },
    ]);
    setBuilder(false);
  }
  return (
    <>
      <Head
        label="WORKFLOW DESIGN"
        title="Automations"
        desc="Sketch a routine without scheduling or executing it."
        action={
          <button
            type="button"
            className={solid}
            onClick={() => setBuilder(!builder)}
            aria-expanded={builder}
          >
            {builder ? 'Close builder' : '＋ New draft'}
          </button>
        }
      />
      <Unavailable
        title="Automation service unavailable"
        reason="The Web automation feature is disabled or not mounted on this backend. Drafts stay on this page until you leave; they are not saved, scheduled, enabled or run on your host."
      />
      {builder && (
        <Panel cls="th-enter mt-5 max-w-[780px] p-6">
          <h2 className="font-semibold">Create an unscheduled draft</h2>
          <form onSubmit={saveDraft} className="mt-5 space-y-4">
            <label className="block text-xs">
              Routine name
              <input
                name="name"
                className={field + ' mt-2'}
                required
                maxLength={120}
                onInput={event => event.currentTarget.setCustomValidity('')}
                placeholder="Daily development review"
              />
            </label>
            <div className="grid grid-cols-2 gap-4 max-md:grid-cols-1">
              <label className="text-xs">
                Trigger
                <select name="trigger" className={field + ' mt-2'}>
                  <option>Manual</option>
                  <option>Every evening</option>
                  <option>When a task completes</option>
                </select>
              </label>
              <label className="text-xs">
                Action
                <select name="action" className={field + ' mt-2'}>
                  <option>Summarize tasks</option>
                  <option>Review paper trading</option>
                  <option>Run approved tests</option>
                </select>
              </label>
            </div>
            <label className="block text-xs">
              Condition (optional)
              <input
                name="condition"
                className={field + ' mt-2'}
                maxLength={180}
                placeholder="Only on weekdays"
              />
            </label>
            <label className="flex items-center gap-2 text-xs">
              <input type="checkbox" name="approval" defaultChecked /> Require approval in draft
            </label>
            <button type="submit" className={solid}>
              Keep draft on this page
            </button>
          </form>
        </Panel>
      )}
      <Panel cls="mt-5 p-6">
        <h2 className="font-semibold">Your drafts</h2>
        {drafts.length === 0 && (
          <p className={`py-10 text-center ${muted}`}>
            No drafts yet. Open the builder to try the form.
          </p>
        )}
        {drafts.map(draft => (
          <Row
            key={draft.id}
            main={draft.name}
            sub={`${draft.trigger} → ${draft.action}${draft.condition ? ` · ${draft.condition}` : ''} · ${draft.approval ? 'Approval requested in draft' : 'No approval in draft'}`}
            right={
              <div className="flex flex-wrap items-center gap-2">
                <Chip>LOCAL DRAFT</Chip>
                <button
                  type="button"
                  className={outline}
                  aria-label={`Remove ${draft.name}`}
                  onClick={() => setDrafts(items => items.filter(item => item.id !== draft.id))}
                >
                  Remove
                </button>
              </div>
            }
          />
        ))}
      </Panel>
    </>
  );
}

export function Settings({
  userId,
  expiresAt,
  checkedAt,
  connected,
  reduced,
  motionProblem,
  toggleMotion,
  checkSession,
  logout,
}: {
  userId: string;
  expiresAt: number;
  checkedAt: number;
  connected: boolean;
  reduced: boolean;
  motionProblem: string;
  toggleMotion: () => void;
  checkSession: () => void;
  logout: () => void;
}) {
  return (
    <>
      <Head
        label="PERSONALIZATION"
        title="Settings"
        desc="Manage this browser session, your visual preferences and integration visibility."
      />
      <div className="grid max-w-[1100px] min-w-0 items-start gap-4 xl:grid-cols-2">
        <div className="min-w-0 space-y-4">
          <Panel cls="p-5">
            <h2 className="font-semibold">Visual preferences</h2>
            <Row main="Theme" sub="Graphite, silver and pearl" right="Monochrome" />
            <Row
              main="Reduced motion"
              sub="Turn off water and page transitions. The system preference is always respected."
              right={
                <button
                  type="button"
                  className={outline}
                  aria-pressed={reduced}
                  onClick={toggleMotion}
                >
                  {reduced ? 'Reduce motion: on' : 'Reduce motion: off'}
                </button>
              }
            />
            {motionProblem && (
              <p role="status" className={muted}>
                {motionProblem}
              </p>
            )}
          </Panel>
          <Panel cls="p-5">
            <h2 className="font-semibold">Integrations and permissions</h2>
            <Row
              main="Paytm Money and market providers"
              sub="No brokerage account or data provider is linked."
              right="Unavailable"
            />
            <Row
              main="Device permissions"
              sub="Review and revoke explicitly mapped pairings. Enrollment and permissions policy are managed outside Web."
              right={
                <a className={`${outline} inline-block`} href="#/device-hub">
                  Open Device Hub
                </a>
              }
            />
            <Row
              main="Notification preferences"
              sub="There is no browser notification settings or history API."
              right="Unavailable"
            />
          </Panel>
        </div>
        <Panel cls="order-first min-w-0 p-5 xl:order-last">
          <h2 className="font-semibold">Browser session</h2>
          <Row
            main="Web API v1"
            sub="Session verification, not Mac or model health."
            right={<Chip>{connected ? 'SESSION VERIFIED' : 'CONNECTION UNCERTAIN'}</Chip>}
          />
          <p role="status" className="my-2 text-xs text-[#c7c8ce]">
            Last verified at {new Date(checkedAt).toLocaleTimeString()}.
          </p>
          <Row
            main="Workspace owner"
            sub={userId}
            right={`Expires ${formatDate(expiresAt * 1000)}`}
          />
          <div className="mt-4 flex flex-wrap gap-2">
            <button type="button" className={outline} onClick={checkSession}>
              Check session
            </button>
            <button type="button" className={solid} onClick={logout}>
              Sign out
            </button>
          </div>
        </Panel>
      </div>
    </>
  );
}
