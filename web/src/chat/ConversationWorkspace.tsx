import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from 'react';
import { createPortal } from 'react-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  errorText,
  mayHaveBeenAccepted,
  WebApiError,
  type Conversation,
  type ConversationEvent,
  type WebMessage,
  type WorkspaceApi,
} from '../api/client';
import type { WebConversationStreamMode } from '../../../contracts/web-api/v1';
import { Chip, Loading, Notice } from '../components/ui';
import { formatDate, muted, outline, solid } from '../components/styles';
import { MarkdownMessage } from './MarkdownMessage';
import { LivingPulseMark } from '../auth/LivingPulseStartup';
import {
  conversationListKey as listKey,
  detailKey,
  isPendingResponse,
  useConversationSnapshot,
} from './updates';
import type { ConversationAction } from './ConversationActions';
import { CommandResult } from './CommandResult';
import { parseSlash, slashCommands, titleFromMessage } from './slashCommands';

const maxMessageLength = 16_000;
const starters = [
  ['Explore an idea', 'Help me explore an idea: '],
  ['Write some code', 'Help me write code to '],
  ['Start a task', '/task '],
] as const;
const eventLabels: Record<ConversationEvent['event_type'], string> = {
  accepted: 'Message accepted',
  queued: 'Response queued',
  started: 'Generation started',
  completed: 'Response completed',
  failed: 'Generation failed',
  cancelled: 'Response cancelled',
  cancellation_requested: 'Cancellation requested',
};
const failures: Record<string, string> = {
  engine_unavailable: 'The Thozha host has no conversation model available.',
  empty_engine_response: 'The configured model returned an empty response.',
  generation_failed: 'The configured model could not complete the response.',
  process_interrupted: 'The host restarted before the response completed.',
};
type Attempt = { id: string; key: string; text: string };
type SendIssue = { attempt: Attempt; message: string; uncertain: boolean };
function omitConversation<T>(entries: Record<string, T>, id: string) {
  if (!(id in entries)) return entries;
  const next = { ...entries };
  delete next[id];
  return next;
}
type LiveResponse = {
  conversationId: string;
  responseId: string;
  mode?: WebConversationStreamMode;
  partial: string;
};

function Message({
  message,
  cancellation,
  cancel,
  reuse,
  live,
}: {
  message: WebMessage;
  cancellation: boolean;
  cancel?: () => void;
  reuse?: () => void;
  live?: LiveResponse;
}) {
  if (message.role === 'user') {
    return (
      <div className="th-v4-user-message ml-auto max-w-[85%] rounded-2xl rounded-br-sm border border-white/15 bg-[#2b2c32] px-4 py-3 text-[13px]">
        <span className="sr-only">You: </span>
        <p className="whitespace-pre-wrap break-words">{message.content}</p>
      </div>
    );
  }
  return (
    <div className="th-v4-assistant-message max-w-[95%] text-[13px] leading-7 [content-visibility:auto] [contain-intrinsic-size:auto_64px]">
      <div className="mb-2 text-[10px] font-bold tracking-widest text-[#aaacb5]">THOZHA</div>
      {message.status === 'completed' && <MarkdownMessage content={message.content} />}
      {isPendingResponse(message.status) && (
        <div className="flex flex-wrap items-center gap-3">
          <span role="status" className={muted}>
            {cancellation
              ? 'Cancellation requested. Waiting for the host.'
              : live?.mode === 'buffered'
                ? 'Buffered response · waiting for the complete result.'
                : live?.partial
                  ? 'Receiving response…'
                  : message.status === 'queued'
                    ? 'Response queued…'
                    : 'Generating response…'}
          </span>
          {!cancellation && (
            <button
              type="button"
              className="grid size-8 place-items-center rounded-md text-[#c7c8ce] hover:bg-white/10 disabled:opacity-50"
              onClick={cancel}
              disabled={!cancel}
              aria-label="Stop generating"
              title="Stop generating"
            >
              <span aria-hidden="true" className="size-2.5 rounded-[2px] bg-current" />
            </button>
          )}
        </div>
      )}
      {isPendingResponse(message.status) && !cancellation && live?.partial && (
        <p className="mt-2 whitespace-pre-wrap break-words" aria-label="Partial response">
          {live.partial}
        </p>
      )}
      {message.status === 'cancelled' && <p className={muted}>Response cancelled.</p>}
      {message.status === 'failed' && (
        <div className="flex flex-wrap items-center gap-2">
          <p className={muted}>
            {failures[message.error_code ?? ''] ?? 'The response failed on the host.'}
          </p>
          {reuse && (
            <button type="button" className={outline} onClick={reuse}>
              Reuse prompt
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export default function ConversationWorkspace({
  api,
  active,
  connected,
  reduced,
  onReviewTask,
  inspector,
  titleSlot,
  onManageConversation,
  changedConversationId,
}: {
  api: WorkspaceApi;
  active: boolean;
  connected: boolean;
  reduced: boolean;
  onReviewTask: (objective: string) => void;
  inspector: HTMLElement | null;
  titleSlot: HTMLElement | null;
  onManageConversation: (action: ConversationAction) => void;
  changedConversationId?: string;
}) {
  const cache = useQueryClient();
  const [selection, setSelection] = useState<string | null>(null);
  const [archived, setArchived] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [issues, setIssues] = useState<Record<string, SendIssue | undefined>>({});
  const [receipts, setReceipts] = useState<Record<string, string | undefined>>({});
  const [live, setLive] = useState<LiveResponse>();
  const [fresh, setFresh] = useState(false);
  const [firstDraft, setFirstDraft] = useState('');
  const [slashError, setSlashError] = useState('');
  const [commandResult, setCommandResult] = useState<'status' | 'help'>();
  const [paletteDismissed, setPaletteDismissed] = useState(false);
  const [paletteIndex, setPaletteIndex] = useState(0);
  const [cancelError, setCancelError] = useState<{ id: string; message: string }>();
  const [notice, setNotice] = useState('');
  const log = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const composerOrigin = useRef<number | undefined>(undefined);
  const focusDraft = useRef(false);
  const createLock = useRef(false);
  const createUncertain = useRef(false);
  const followLatest = useRef(true);
  const previousConversation = useRef<string | null>(null);
  const activeSend = useRef(new Set<string>());
  const activeStream = useRef<AbortController | null>(null);
  const fieldId = useId();
  const enabled = active && connected;
  useEffect(() => () => activeStream.current?.abort(), []);
  useEffect(() => {
    if (!changedConversationId) return;
    setSelection(previous => (previous === changedConversationId ? null : previous));
    setDrafts(previous => omitConversation(previous, changedConversationId));
    setIssues(previous => omitConversation(previous, changedConversationId));
    setReceipts(previous => omitConversation(previous, changedConversationId));
  }, [changedConversationId]);
  useEffect(() => {
    function selectFromLocation() {
      if (!window.location.hash.startsWith('#/ai-workspace')) return;
      const params = new URLSearchParams(window.location.hash.split('?')[1]);
      if (params.has('new')) {
        startConversation();
        return;
      }
      const requested = params.get('conversation');
      if (requested) {
        setFresh(false);
        setSlashError('');
        setArchived(params.get('archive') === '1');
        setSelection(requested);
      }
    }
    selectFromLocation();
    window.addEventListener('hashchange', selectFromLocation);
    return () => window.removeEventListener('hashchange', selectFromLocation);
  }, []);

  const list = useQuery({ queryKey: listKey, queryFn: ({ signal }) => api.list(signal), enabled });
  const items = list.data?.items.filter(item => Boolean(item.archived_at) === archived) ?? [];
  const selected = fresh ? undefined : (items.find(item => item.id === selection) ?? items[0]);
  const id = selected?.id ?? null;
  const detail = useConversationSnapshot(
    api.get,
    id,
    enabled,
    id ? receipts[id] : undefined,
    live?.conversationId === id
  );
  const snapshot = detail.data;
  const messages = snapshot?.messages;
  const prompts = useMemo(
    () =>
      new Map(
        messages
          ?.filter(message => message.role === 'user')
          .map(message => [message.id, message.content])
      ),
    [messages]
  );
  const writingNew = !archived && !id && (fresh || list.isSuccess);
  const welcome =
    !archived &&
    (writingNew || snapshot?.messages.length === 0) &&
    !(id && (receipts[id] || live?.conversationId === id));
  const draft = id ? (drafts[id] ?? '') : writingNew ? firstDraft : '';
  const slash = parseSlash(draft);
  const candidates = slash?.completing
    ? slashCommands.filter(entry => !('deferred' in entry) && entry.name.startsWith(slash.name))
    : [];
  const paletteOpen = Boolean(active && !paletteDismissed && slash?.completing);
  const highlighted = candidates[paletteIndex] ?? candidates[0];
  const paletteId = `${fieldId}-commands`;
  const issue = id ? issues[id] : undefined;
  const receipt = id ? receipts[id] : undefined;
  const awaitingHistory = Boolean(receipt && !messages?.some(message => message.id === receipt));
  const pending = Boolean(
    messages?.some(message => message.role === 'assistant' && isPendingResponse(message.status))
  );
  const canWrite = Boolean(
    !archived && connected && (writingNew || (id && snapshot && !detail.isError))
  );

  useLayoutEffect(() => {
    if (welcome) return;
    const origin = composerOrigin.current;
    composerOrigin.current = undefined;
    const form = composer.current?.form;
    if (
      !form ||
      origin === undefined ||
      reduced ||
      matchMedia('(prefers-reduced-motion: reduce)').matches
    )
      return;
    const offset = origin - form.getBoundingClientRect().top;
    if (Math.abs(offset) < 2) return;
    const animation = form.animate([{ translate: `0 ${offset}px` }, { translate: '0 0' }], {
      duration: 360,
      easing: 'cubic-bezier(.22,.8,.27,1)',
    });
    return () => animation.cancel();
  }, [welcome, reduced]);

  useLayoutEffect(() => {
    if (active && canWrite && focusDraft.current && composer.current) {
      focusDraft.current = false;
      composer.current.focus({ preventScroll: true });
    }
  });
  const pendingCancellation = new Set(
    snapshot?.events
      .filter(event => event.event_type === 'cancellation_requested')
      .map(event => event.message_id)
  );

  useLayoutEffect(() => {
    if (!active || !log.current) return;
    const switched = previousConversation.current !== id;
    previousConversation.current = id;
    if (switched) followLatest.current = true;
    if (followLatest.current)
      log.current.scrollTo({
        top: log.current.scrollHeight,
        behavior:
          switched || reduced || matchMedia('(prefers-reduced-motion: reduce)').matches
            ? 'instant'
            : 'smooth',
      });
  }, [active, awaitingHistory, id, live?.partial, messages, reduced]);

  const command = useMutation({
    mutationFn: (text: string) => api.create({ title: titleFromMessage(text) }),
    onSuccess: async (created, text) => {
      await cache.cancelQueries({ queryKey: listKey });
      if (active) measureComposerOrigin();
      cache.setQueryData<{ items: Conversation[] }>(listKey, initial => ({
        items: [created, ...(initial?.items ?? []).filter(item => item.id !== created.id)],
      }));
      setDrafts(previous => ({ ...previous, [created.id]: text }));
      setFirstDraft('');
      setFresh(false);
      setArchived(false);
      setSelection(created.id);
      if (window.location.hash.startsWith('#/ai-workspace') || !window.location.hash)
        window.location.hash = `#/ai-workspace?conversation=${encodeURIComponent(created.id)}`;
      beginSend({ id: created.id, key: crypto.randomUUID(), text });
      void cache.invalidateQueries({ queryKey: listKey });
    },
    onError: error => {
      const guidance = mayHaveBeenAccepted(error)
        ? ' The server may have saved this conversation. Check the sidebar before starting a new chat.'
        : '';
      createUncertain.current = Boolean(guidance);
      setSlashError(errorText(error, 'create the conversation') + guidance);
      if (guidance) void cache.invalidateQueries({ queryKey: listKey });
    },
    onSettled: () => {
      createLock.current = false;
    },
  });

  function recordAcceptance(attempt: Attempt, responseId: string) {
    if (active && welcome && id === attempt.id) measureComposerOrigin();
    followLatest.current = true;
    setReceipts(previous => ({ ...previous, [attempt.id]: responseId }));
    setIssues(previous => ({ ...previous, [attempt.id]: undefined }));
    setDrafts(previous => ({ ...previous, [attempt.id]: '' }));
    void cache.invalidateQueries({ queryKey: detailKey(attempt.id) });
    void cache.invalidateQueries({ queryKey: listKey });
  }

  const send = useMutation({
    mutationFn: async ({ attempt, recovery }: { attempt: Attempt; recovery: boolean }) => {
      const body = { content: attempt.text };
      if (!recovery) {
        const controller = new AbortController();
        activeStream.current = controller;
        let acknowledged = false;
        try {
          await api.stream(attempt.id, body, attempt.key, controller.signal, event => {
            switch (event.event) {
              case 'accepted':
                acknowledged = true;
                setLive({
                  conversationId: attempt.id,
                  responseId: event.data.response_id,
                  partial: '',
                });
                recordAcceptance(attempt, event.data.response_id);
                break;
              case 'stream_mode':
                setLive(current => (current ? { ...current, mode: event.data.mode } : current));
                break;
              case 'delta':
                setLive(current =>
                  current?.mode === 'engine_stream'
                    ? { ...current, partial: current.partial + event.data.content }
                    : current
                );
                break;
            }
          });
          return;
        } catch (error) {
          if (controller.signal.aborted) throw error;
          if (acknowledged) {
            setNotice(
              'The response connection ended early. Checking saved status; no message was resent.'
            );
            return;
          }
          if (!(error instanceof WebApiError && (error.status === 404 || error.status === 405)))
            throw error;
          setNotice(
            'Streaming is unavailable on this host. Checking the response in saved history.'
          );
        } finally {
          activeStream.current = null;
          setLive(undefined);
        }
      }
      const accepted = await api.submit(attempt.id, body, attempt.key);
      recordAcceptance(attempt, accepted.response_id);
    },
    onError: (error, { attempt }) => {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      const uncertain = mayHaveBeenAccepted(error);
      setIssues(previous => ({
        ...previous,
        [attempt.id]: { attempt, uncertain, message: errorText(error, 'send the message') },
      }));
    },
    onSettled: (_data, _error, { attempt }) => {
      activeSend.current.delete(attempt.id);
      void cache.invalidateQueries({ queryKey: detailKey(attempt.id) });
    },
  });

  const cancel = useMutation({
    mutationFn: ({ conversationId, responseId }: { conversationId: string; responseId: string }) =>
      api.cancel(conversationId, responseId),
    onMutate: () => setCancelError(undefined),
    onSuccess: result => {
      setNotice(
        result.status === 'cancellation_requested'
          ? 'Cancellation requested. The model call may still be running.'
          : result.status === 'cancelled'
            ? 'Response cancelled.'
            : `The response is already ${result.status}.`
      );
    },
    onError: (error, { conversationId }) =>
      setCancelError({ id: conversationId, message: errorText(error, 'cancel the response') }),
    onSettled: (_data, _error, { conversationId }) => {
      void cache.invalidateQueries({ queryKey: detailKey(conversationId) });
    },
  });

  const sending = send.isPending;
  const composerDisabled =
    !canWrite ||
    command.isPending ||
    sending ||
    awaitingHistory ||
    pending ||
    Boolean(issue?.uncertain);

  function beginSend(attempt: Attempt, recovery = false) {
    if (activeSend.current.size || !connected) return;
    activeSend.current.add(attempt.id);
    setNotice('');
    send.mutate({ attempt, recovery });
  }

  function measureComposerOrigin() {
    composerOrigin.current = composer.current?.form?.getBoundingClientRect().top;
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = draft.trim();
    if (composerDisabled || !value || createLock.current) return;
    setPaletteDismissed(true);
    if (slash) {
      if (!slash.command || 'deferred' in slash.command) {
        setSlashError(
          slash.command
            ? `/${slash.name} is not enabled. Use /help for available commands.`
            : 'Unknown command. Use /help for available commands.'
        );
        return;
      }
      if ((slash.command.name === 'task') !== Boolean(slash.argument)) {
        setSlashError(`Usage: ${slash.command.usage}`);
        return;
      }
      if (slash.command.name === 'task') {
        setSlashError('');
        onReviewTask(slash.argument);
      } else {
        updateDraft('');
        setCommandResult(slash.command.name);
      }
      return;
    }
    if (id) beginSend({ id, key: crypto.randomUUID(), text: value });
    else if (writingNew && !createUncertain.current && !activeSend.current.size) {
      createLock.current = true;
      setSlashError('');
      command.mutate(value);
    }
  }

  function startConversation() {
    if (createLock.current) return;
    focusDraft.current = true;
    createUncertain.current = false;
    composerOrigin.current = undefined;
    setFresh(true);
    setArchived(false);
    setFirstDraft('');
    setNotice('');
    setSlashError('');
    setPaletteDismissed(false);
    setPaletteIndex(0);
    composer.current?.focus({ preventScroll: true });
  }

  function updateDraft(text: string) {
    if (id) setDrafts(previous => ({ ...previous, [id]: text }));
    else if (writingNew) {
      setFresh(true);
      setFirstDraft(text);
    }
    setSlashError('');
    setPaletteDismissed(false);
    setPaletteIndex(0);
  }

  function completeCommand(name: string) {
    updateDraft(`/${name} `);
    setPaletteDismissed(true);
    composer.current?.focus({ preventScroll: true });
  }

  const selectedName = snapshot?.title ?? selected?.title ?? (archived ? 'Archive' : 'New chat');

  return (
    <>
      {active &&
        titleSlot &&
        createPortal(
          <h1
            data-testid="chat-title"
            className="truncate text-sm font-medium"
            title={selectedName}
          >
            {selectedName}
          </h1>,
          titleSlot
        )}
      <div className="th-v4-chat-workspace">
        <div
          className={`th-v4-conversation flex min-h-0 min-w-0 flex-col overflow-hidden ${welcome ? 'is-welcome' : ''}`}
        >
          <p role="status" className={notice || archived ? `px-5 pt-3 ${muted}` : 'sr-only'}>
            {notice || (archived ? 'Archived · read-only' : '')}
          </p>
          {list.isPending && connected && !fresh && <Loading label="Loading conversations…" />}
          {list.isError && (
            <div className="px-4 pt-3">
              <Notice error retry={connected ? () => void list.refetch() : undefined}>
                {errorText(list.error, 'load conversations')}
              </Notice>
            </div>
          )}
          {id && detail.isError && (
            <div className="px-4 pt-4">
              <Notice error retry={connected ? () => void detail.refetch() : undefined}>
                {errorText(detail.error, 'load history')}
              </Notice>
            </div>
          )}
          {id && cancelError?.id === id && (
            <div className="px-4 pt-4">
              <Notice error>{cancelError.message}</Notice>
            </div>
          )}
          <div
            ref={log}
            aria-label="Conversation messages"
            role="region"
            tabIndex={snapshot?.messages.length ? 0 : undefined}
            onScroll={event => {
              const el = event.currentTarget;
              followLatest.current = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
            }}
            className="th-v4-message-log flex min-h-[230px] flex-1 flex-col gap-7 overflow-y-auto p-5"
          >
            {welcome && (
              <div className="th-v4-empty th-v4-welcome text-center">
                <div className="th-v4-welcome-mark" aria-hidden="true">
                  <LivingPulseMark />
                </div>
                <h2>What's on your mind?</h2>
                <p>An idea, a question, or something worth building.</p>
              </div>
            )}
            {id && detail.isPending && (
              <Loading
                label={
                  connected ? 'Loading saved history…' : 'History will load after reconnection.'
                }
              />
            )}
            {archived && list.isSuccess && (!id || snapshot?.messages.length === 0) && (
              <div className="th-v4-empty m-auto text-center">
                <h2>{id ? 'No messages in this conversation.' : 'No archived conversations.'}</h2>
              </div>
            )}
            {messages?.map(message => {
              const firstPrompt = message.response_to
                ? prompts.get(message.response_to)
                : undefined;
              return (
                <Message
                  key={message.id}
                  message={message}
                  cancellation={pendingCancellation.has(message.id)}
                  live={live?.responseId === message.id ? live : undefined}
                  cancel={
                    connected && id && !cancel.isPending
                      ? () => cancel.mutate({ conversationId: id, responseId: message.id })
                      : undefined
                  }
                  reuse={
                    firstPrompt && id && canWrite && !composerDisabled
                      ? () => {
                          setDrafts(previous => ({ ...previous, [id]: firstPrompt }));
                          composer.current?.focus();
                        }
                      : undefined
                  }
                />
              );
            })}
            {awaitingHistory && (
              <p role="status" className={muted}>
                Message accepted. Waiting for its saved response status…
              </p>
            )}
          </div>
          {issue && (
            <div className="mx-4">
              <Notice error>
                {issue.message}
                {issue.uncertain &&
                  ' The server may have accepted it. A safe retry uses the original submission key.'}
              </Notice>
              {issue.uncertain && (
                <div className="my-2 flex flex-wrap gap-2">
                  <button
                    className={outline}
                    type="button"
                    disabled={!connected || send.isPending}
                    onClick={() => beginSend(issue.attempt, true)}
                  >
                    Retry send safely
                  </button>
                  <button
                    className={outline}
                    type="button"
                    disabled={send.isPending}
                    onClick={() => {
                      setIssues(previous => ({ ...previous, [issue.attempt.id]: undefined }));
                      setDrafts(previous => ({ ...previous, [issue.attempt.id]: '' }));
                      void cache.invalidateQueries({ queryKey: detailKey(issue.attempt.id) });
                      setNotice(
                        'Draft discarded. Check saved history to see whether the message was accepted.'
                      );
                    }}
                  >
                    Discard draft and check history
                  </button>
                </div>
              )}
            </div>
          )}
          {!archived && (id || writingNew) && (
            <form
              data-intro-composer
              onSubmit={submit}
              className="m-4 rounded-2xl border border-white/20 bg-[#222329] p-2"
            >
              {paletteOpen && (
                <div className="mb-2 rounded-xl border border-white/15 bg-[#1c1d22] px-2 py-1 text-xs">
                  <p className="px-2 py-1 text-[#c4c5cb]">Composer commands</p>
                  <ul
                    id={paletteId}
                    role="listbox"
                    aria-label="Composer commands"
                    className="max-h-[min(30dvh,12rem)] overflow-y-auto"
                  >
                    {candidates.map(entry => (
                      <li
                        key={entry.name}
                        id={`${paletteId}-${entry.name}`}
                        role="option"
                        aria-selected={highlighted?.name === entry.name}
                        className="cursor-pointer rounded-md px-2 py-2 aria-selected:bg-white/15 hover:bg-white/10"
                        onMouseDown={event => event.preventDefault()}
                        onClick={() => completeCommand(entry.name)}
                      >
                        <code>{entry.usage}</code>
                        <span className="ml-2 text-[#c4c5cb]">{entry.description}</span>
                      </li>
                    ))}
                  </ul>
                  {!candidates.length && (
                    <p role="status" className="px-2 py-1">
                      No matching commands. Use /help for the command list.
                    </p>
                  )}
                </div>
              )}
              {slashError && <Notice error>{slashError}</Notice>}
              {slash?.command && !('deferred' in slash.command) && (
                <div className="px-2 pt-1">
                  <Chip>{slash.command.usage} · Command</Chip>
                </div>
              )}
              <textarea
                ref={composer}
                rows={2}
                maxLength={maxMessageLength}
                value={draft}
                readOnly={!canWrite || command.isPending || sending || Boolean(issue?.uncertain)}
                onChange={event => updateDraft(event.target.value)}
                onKeyDown={event => {
                  if (event.nativeEvent.isComposing) return;
                  if (paletteOpen && event.key === 'Escape') {
                    event.preventDefault();
                    event.stopPropagation();
                    setPaletteDismissed(true);
                    return;
                  }
                  if (paletteOpen && highlighted && ['ArrowUp', 'ArrowDown'].includes(event.key)) {
                    event.preventDefault();
                    const offset = event.key === 'ArrowUp' ? -1 : 1;
                    const next = (paletteIndex + offset + candidates.length) % candidates.length;
                    setPaletteIndex(next);
                    document
                      .getElementById(`${paletteId}-${candidates[next].name}`)
                      ?.scrollIntoView({ block: 'nearest' });
                    return;
                  }
                  if (
                    paletteOpen &&
                    highlighted &&
                    !event.shiftKey &&
                    ['Enter', 'Tab'].includes(event.key)
                  ) {
                    event.preventDefault();
                    completeCommand(highlighted.name);
                    return;
                  }
                  if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    event.currentTarget.form?.requestSubmit();
                  }
                }}
                placeholder="Message Thozha…"
                aria-label="Message Jarvis"
                aria-describedby={fieldId}
                aria-autocomplete="list"
                aria-controls={paletteOpen ? paletteId : undefined}
                aria-activedescendant={
                  paletteOpen && highlighted ? `${paletteId}-${highlighted.name}` : undefined
                }
                className="min-h-[48px] w-full resize-y rounded-lg bg-transparent px-3 py-2 text-xs leading-6 text-white placeholder:text-[#a4a5af]"
              />
              <div className="flex flex-wrap items-center justify-between gap-2 px-2 pb-1">
                <span id={fieldId} className="text-[10px] text-[#a4a5af]">
                  {paletteOpen
                    ? 'Arrow keys to navigate · Enter or Tab to select · Escape to close'
                    : 'Enter to send · Shift+Enter for newline · / for commands'}
                </span>
                <button
                  disabled={
                    composerDisabled ||
                    !draft.trim() ||
                    (writingNew && createUncertain.current && !slash)
                  }
                  className={solid}
                  type="submit"
                >
                  {sending || command.isPending
                    ? 'Sending…'
                    : slash?.command?.name === 'task'
                      ? 'Review task'
                      : slash
                        ? 'Run command'
                        : 'Send ↑'}
                </button>
              </div>
            </form>
          )}
          {welcome && (
            <div className="th-v4-starters" aria-label="Conversation starters" role="group">
              {starters.map(([label, prompt]) => (
                <button
                  key={label}
                  type="button"
                  disabled={composerDisabled || Boolean(draft.trim())}
                  onClick={() => {
                    updateDraft(prompt);
                    composer.current?.focus({ preventScroll: true });
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
          )}
        </div>
        {inspector &&
          createPortal(
            <div className="th-v4-chat-context">
              <div className="mb-4 flex flex-wrap justify-between gap-2">
                <h2 className="text-xs font-semibold">Context</h2>
              </div>
              <div className="rounded-xl border border-white/10 bg-white/[.04] p-4">
                <b className="text-xs">Conversation only</b>
                <p className={`mt-2 ${muted}`}>
                  Jarvis can use this conversation’s history. Browser chat cannot run Mac tools.
                </p>
                <p className={`mt-2 ${muted}`}>
                  {connected ? 'Browser session verified.' : 'Browser session not verified.'}{' '}
                  {id && live?.conversationId === id && live.mode === 'buffered'
                    ? 'The current reply is delivered in full when ready.'
                    : 'Incremental replies require a streaming model.'}
                </p>
                <button
                  type="button"
                  className={`${outline} mt-3`}
                  disabled={!connected}
                  onClick={() => onReviewTask(slash ? '' : draft.trim())}
                >
                  Review as supervisor task
                </button>
                <p className={`mt-2 ${muted}`}>
                  Open a separate task form, optionally with your current draft. Nothing is
                  submitted automatically.
                </p>
              </div>
              {snapshot && (
                <details className="mt-4">
                  <summary className="text-xs font-semibold">
                    Response events ({snapshot.events.length})
                  </summary>
                  {snapshot.events.length ? (
                    <ol className="mt-3 max-h-64 space-y-2 overflow-auto border-l border-white/20 pl-3">
                      {snapshot.events.map(event => (
                        <li key={event.sequence} className="text-[11px] text-[#c4c5cb]">
                          <p>{eventLabels[event.event_type]}</p>
                          <time className="text-[10px] text-[#a4a5af]" dateTime={event.created_at}>
                            {formatDate(event.created_at)}
                          </time>
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <p className={`mt-3 ${muted}`}>No response events yet.</p>
                  )}
                </details>
              )}
              {selected && snapshot && (
                <div className="mt-4 flex flex-wrap gap-2">
                  {!archived && (
                    <button
                      type="button"
                      disabled={!connected || pending || awaitingHistory || sending}
                      onClick={() =>
                        onManageConversation({ type: 'archive', conversation: selected })
                      }
                      className={outline}
                    >
                      Archive
                    </button>
                  )}
                  <button
                    type="button"
                    disabled={!connected || pending || awaitingHistory || sending}
                    onClick={() => onManageConversation({ type: 'delete', conversation: selected })}
                    className={outline}
                  >
                    Delete
                  </button>
                </div>
              )}
            </div>,
            inspector
          )}
      </div>
      {active && commandResult && (
        <CommandResult mode={commandResult} api={api} close={() => setCommandResult(undefined)} />
      )}
    </>
  );
}
