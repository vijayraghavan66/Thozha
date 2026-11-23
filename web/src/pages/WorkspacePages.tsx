import { useState, type FormEvent } from 'react';
import { Chip, Head, Line, Panel, Row, Stat, Unavailable } from '../components/ui';
import { LiquidCard } from '../components/LiquidSurface';
import { WaterSurface } from '../components/WaterSurface';
import { field, formatDate, muted, outline, solid } from '../components/styles';

export function Overview({
  onChat,
  onDevices,
  onSettings,
  connected,
  reduced,
}: {
  onChat: () => void;
  onDevices: () => void;
  onSettings: () => void;
  connected: boolean;
  reduced: boolean;
}) {
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
        <Stat label="TASKS" value="—" detail="No browser task API" />
        <Stat label="DEVICES" value="—" detail="No browser device API" />
        <Stat label="APPROVALS" value="—" detail="Not exposed to Web" />
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
            desc="Browser sessions do not have access to supervisor tasks or approvals."
            flag="Unavailable"
          />
          <button type="button" className={outline + ' mt-3'} onClick={onChat}>
            Open conversations
          </button>
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
            desc="The companion API is outside the browser authorization boundary."
            flag="Unavailable"
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

export function DeviceHub() {
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
        reason="The browser API does not expose discovery, pairing, status, permissions or revocation. This page never scans your network."
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
export function Blueprint() {
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
        reason="Web v1 has no mounted scheduler API. Drafts stay on this page until you leave; they are not saved, scheduled, enabled or run on your host."
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

export function DeveloperStudio() {
  return (
    <>
      <Head
        label="EXECUTION / EVIDENCE"
        title="Developer Studio"
        desc="Inspect which developer operations are available in this browser session."
      />
      <div className="grid grid-cols-3 gap-3 max-md:grid-cols-1">
        <Stat label="REPOSITORY" value="Thozha" detail="Workspace name; no GitHub connection" />
        <Stat label="CI" value="—" detail="Build and test results not connected" />
        <Stat label="SUPERVISOR TASKS" value="—" detail="No browser task API" />
      </div>
      <div className="mt-5 grid grid-cols-2 gap-4 max-lg:grid-cols-1">
        <Unavailable
          title="Task timeline"
          reason="Development jobs, execution logs, builds and artifact delivery are not exposed to Web sessions. Chat responses are not Mac task execution."
        >
          <button type="button" className={outline} disabled>
            Run project checks
          </button>
        </Unavailable>
        <Unavailable
          title="Review and approvals"
          reason="This browser session cannot read or decide supervisor approvals. No Mac permission can be granted here."
        />
      </div>
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
              sub="Managed through the separate companion authorization boundary."
              right="Unavailable"
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
