import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from 'react';
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
import { Chip, Head, Loading, Modal, Notice, Panel } from '../components/ui';
import { field, formatDate, muted, outline, solid } from '../components/styles';
import { MarkdownMessage } from './MarkdownMessage';
import { detailKey, isPendingResponse, useConversationSnapshot } from './updates';

const listKey = ['conversations'] as const;
const maxMessageLength = 16_000;
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
type Dialog = { type: 'create' } | { type: 'archive' | 'delete'; conversation: Conversation };
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
      <div className="ml-auto max-w-[85%] rounded-2xl rounded-br-sm border border-white/15 bg-[#2b2c32] px-4 py-3 text-[13px]">
        <span className="sr-only">You: </span>
        <p className="whitespace-pre-wrap break-words">{message.content}</p>
      </div>
    );
  }
  return (
    <div className="max-w-[95%] text-[13px] leading-7 [content-visibility:auto] [contain-intrinsic-size:auto_64px]">
      <div className="mb-2 text-[10px] font-bold tracking-widest text-[#aaacb5]">JARVIS</div>
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
            <button type="button" className={outline} onClick={cancel} disabled={!cancel}>
              Cancel response
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
}: {
  api: WorkspaceApi;
  active: boolean;
  connected: boolean;
  reduced: boolean;
}) {
  const cache = useQueryClient();
  const [selection, setSelection] = useState<string | null>(null);
  const [archived, setArchived] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [issues, setIssues] = useState<Record<string, SendIssue | undefined>>({});
  const [receipts, setReceipts] = useState<Record<string, string | undefined>>({});
  const [live, setLive] = useState<LiveResponse>();
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [commandError, setCommandError] = useState('');
  const [cancelError, setCancelError] = useState<{ id: string; message: string }>();
  const [notice, setNotice] = useState('');
  const log = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const followLatest = useRef(true);
  const previousConversation = useRef<string | null>(null);
  const activeSend = useRef(new Set<string>());
  const activeStream = useRef<AbortController | null>(null);
  const fieldId = useId();
  const enabled = active && connected;
  useEffect(() => () => activeStream.current?.abort(), []);

  const list = useQuery({ queryKey: listKey, queryFn: ({ signal }) => api.list(signal), enabled });
  const items = list.data?.items.filter(item => Boolean(item.archived_at) === archived) ?? [];
  const selected = items.find(item => item.id === selection) ?? items[0];
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
  const draft = id ? (drafts[id] ?? '') : '';
  const issue = id ? issues[id] : undefined;
  const receipt = id ? receipts[id] : undefined;
  const awaitingHistory = Boolean(receipt && !messages?.some(message => message.id === receipt));
  const pending = Boolean(
    messages?.some(message => message.role === 'assistant' && isPendingResponse(message.status))
  );
  const canWrite = Boolean(id && snapshot && !detail.isError && !archived && connected);
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
    mutationFn: async ({ dialog, title }: { dialog: Dialog; title: string }) => {
      if (dialog.type === 'create') return api.create(title ? { title } : {});
      if (dialog.type === 'archive') await api.archive(dialog.conversation.id);
      else await api.delete(dialog.conversation.id);
      return null;
    },
    onSuccess: async (created, { dialog: operation }) => {
      await cache.cancelQueries({ queryKey: listKey });
      if (created) {
        cache.setQueryData<{ items: Conversation[] }>(listKey, initial => ({
          items: [created, ...(initial?.items ?? [])],
        }));
        setArchived(false);
        setSelection(created.id);
        setNotice('Conversation created.');
      } else if (operation.type !== 'create') {
        const removedId = operation.conversation.id;
        cache.setQueryData<{ items: Conversation[] }>(listKey, initial => ({
          items: initial?.items.filter(item => item.id !== removedId) ?? [],
        }));
        cache.removeQueries({ queryKey: detailKey(removedId), exact: true });
        setDrafts(previous => {
          const rest = { ...previous };
          delete rest[removedId];
          return rest;
        });
        setIssues(previous => {
          const rest = { ...previous };
          delete rest[removedId];
          return rest;
        });
        setReceipts(previous => {
          const rest = { ...previous };
          delete rest[removedId];
          return rest;
        });
        setSelection(null);
        setNotice(
          operation.type === 'archive'
            ? 'Conversation archived.'
            : 'Conversation and history deleted.'
        );
      }
      setDialog(null);
      void cache.invalidateQueries({ queryKey: listKey });
    },
    onError: (error, { dialog: operation }) => {
      const guidance =
        operation.type === 'create' && mayHaveBeenAccepted(error)
          ? ' The server may have created it. Close this dialog and check the conversation list before trying again.'
          : '';
      setCommandError(errorText(error, 'change this conversation') + guidance);
      if (guidance) void cache.invalidateQueries({ queryKey: listKey });
    },
  });

  function recordAcceptance(attempt: Attempt, responseId: string) {
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
    !canWrite || sending || awaitingHistory || pending || Boolean(issue?.uncertain);

  function beginSend(attempt: Attempt, recovery = false) {
    if (activeSend.current.size || !connected) return;
    activeSend.current.add(attempt.id);
    setNotice('');
    send.mutate({ attempt, recovery });
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = draft.trim();
    if (!id || composerDisabled || !value) return;
    beginSend({ id, key: crypto.randomUUID(), text: value });
  }

  function openDialog(next: Dialog) {
    setCommandError('');
    setNotice('');
    setDialog(next);
  }
  const selectedName = snapshot?.title ?? selected?.title;

  return (
    <>
      <Head
        label="CONVERSATIONS / WEB v1"
        title="AI Workspace"
        desc="Continue owner-scoped conversations saved on your Thozha host."
        action={
          <button
            type="button"
            className={outline}
            disabled={!connected || list.isPending}
            onClick={() => openDialog({ type: 'create' })}
          >
            ＋ New conversation
          </button>
        }
      />
      <p role="status" className={notice ? `mb-3 ${muted}` : 'sr-only'}>
        {notice}
      </p>
      <div className="grid grid-cols-[200px_minmax(0,1fr)_210px] gap-3 max-xl:grid-cols-[180px_minmax(0,1fr)] max-md:grid-cols-1">
        <Panel cls="min-w-0 p-3">
          <h2 className="px-2 py-2 text-xs font-semibold">Your conversations</h2>
          <label className="flex items-center gap-2 px-2 pb-2 text-xs text-[#c4c5cb]">
            <input
              type="checkbox"
              checked={archived}
              onChange={event => {
                setArchived(event.target.checked);
                setSelection(null);
              }}
            />{' '}
            Show archived
          </label>
          {list.isPending && <Loading label="Loading conversations…" />}
          {list.isError && (
            <Notice error retry={connected ? () => void list.refetch() : undefined}>
              {errorText(list.error, 'load conversations')}
            </Notice>
          )}
          {list.isSuccess && !items.length && (
            <p className={`p-2 ${muted}`}>
              {archived ? 'No archived conversations.' : 'No conversations yet.'}
            </p>
          )}
          {items.length > 0 && (
            <>
              <label className="block px-2 text-xs md:hidden">
                Current conversation
                <select
                  className={field + ' mt-2'}
                  value={id ?? ''}
                  onChange={event => setSelection(event.target.value)}
                >
                  {items.map(item => (
                    <option value={item.id} key={item.id}>
                      {item.title}
                    </option>
                  ))}
                </select>
              </label>
              <div className="hidden max-h-[530px] space-y-1 overflow-y-auto px-1 pb-2 md:block">
                {items.map(item => (
                  <button
                    type="button"
                    key={item.id}
                    aria-pressed={item.id === id}
                    onClick={() => setSelection(item.id)}
                    className={`w-full rounded-xl border px-3 py-3 text-left hover:bg-white/10 ${item.id === id ? 'border-white/25 bg-white/10' : 'border-white/10 bg-white/[.04]'}`}
                  >
                    <span className="block truncate text-xs">{item.title}</span>
                    <span className="mt-2 block text-[10px] text-[#c4c5cb]">
                      {formatDate(item.updated_at)}
                    </span>
                  </button>
                ))}
              </div>
            </>
          )}
        </Panel>
        <Panel cls="flex min-h-[560px] min-w-0 flex-col overflow-hidden">
          <div className="flex flex-wrap items-start justify-between gap-2 border-b border-white/10 p-5">
            <div className="min-w-0">
              <h2 className="break-words text-sm font-semibold">{selectedName ?? 'Jarvis'}</h2>
              <p className="mt-1 text-[11px] text-[#c4c5cb]">
                {archived
                  ? 'Archived · read-only'
                  : live?.conversationId === id && live.mode === 'buffered'
                    ? 'Host conversation · buffered mode'
                    : 'Host conversation · delivery depends on the configured model'}
              </p>
            </div>
            <Chip>{connected ? 'WEB v1' : 'OFFLINE / UNVERIFIED'}</Chip>
          </div>
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
            className="flex max-h-[58dvh] min-h-[335px] flex-1 flex-col gap-5 overflow-y-auto p-5"
          >
            {!id && list.isSuccess && (
              <div className={`m-auto text-center ${muted}`}>
                {archived
                  ? 'Archived conversations appear here.'
                  : 'Create a conversation to start chatting.'}
              </div>
            )}
            {id && detail.isPending && (
              <Loading
                label={
                  connected ? 'Loading saved history…' : 'History will load after reconnection.'
                }
              />
            )}
            {snapshot && !snapshot.messages.length && (
              <div className={`m-auto text-center ${muted}`}>
                What can I help you explore?
                {!archived && (
                  <div className="mt-4 flex flex-wrap justify-center gap-2">
                    {['Explain Java', 'Outline a travel plan'].map(prompt => (
                      <button
                        type="button"
                        className={outline}
                        key={prompt}
                        disabled={!canWrite}
                        onClick={() => {
                          setDrafts(previous => ({ ...previous, [snapshot.id]: prompt }));
                          composer.current?.focus();
                        }}
                      >
                        {prompt}
                      </button>
                    ))}
                  </div>
                )}
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
          {!archived && (
            <form
              onSubmit={submit}
              className="m-4 rounded-2xl border border-white/20 bg-[#222329] p-2"
            >
              <textarea
                ref={composer}
                rows={2}
                maxLength={maxMessageLength}
                value={draft}
                readOnly={!canWrite || sending || Boolean(issue?.uncertain)}
                onChange={event => {
                  if (id) setDrafts(previous => ({ ...previous, [id]: event.target.value }));
                }}
                onKeyDown={event => {
                  if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    event.currentTarget.form?.requestSubmit();
                  }
                }}
                placeholder="Ask Jarvis anything…"
                aria-label="Message Jarvis"
                aria-describedby={fieldId}
                className="min-h-[48px] w-full resize-y rounded-lg bg-transparent px-3 py-2 text-xs leading-6 text-white placeholder:text-[#a4a5af]"
              />
              <div className="flex flex-wrap items-center justify-between gap-2 px-2 pb-1">
                <span id={fieldId} className="text-[10px] text-[#a4a5af]">
                  Enter to send · Shift+Enter for newline
                </span>
                <button
                  disabled={composerDisabled || !draft.trim()}
                  className={solid}
                  type="submit"
                >
                  {sending ? 'Sending…' : 'Send ↑'}
                </button>
              </div>
            </form>
          )}
        </Panel>
        <Panel cls="h-fit min-w-0 p-5 md:max-xl:col-span-2">
          <div className="mb-4 flex flex-wrap justify-between gap-2">
            <h2 className="text-xs font-semibold">Context</h2>
            <Chip>WEB v1</Chip>
          </div>
          <div className="rounded-xl border border-white/10 bg-white/[.04] p-4">
            <b className="text-xs">Conversation only</b>
            <p className={`mt-2 ${muted}`}>
              Jarvis can use this conversation’s history. Browser chat cannot run Mac tools.
              Incremental replies require a streaming model.
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
                  onClick={() => openDialog({ type: 'archive', conversation: selected })}
                  className={outline}
                >
                  Archive
                </button>
              )}
              <button
                type="button"
                disabled={!connected || pending || awaitingHistory || sending}
                onClick={() => openDialog({ type: 'delete', conversation: selected })}
                className={outline}
              >
                Delete
              </button>
            </div>
          )}
        </Panel>
      </div>
      {dialog && (
        <Modal
          title={
            dialog.type === 'create'
              ? 'New conversation'
              : dialog.type === 'archive'
                ? 'Archive conversation'
                : 'Delete conversation'
          }
          locked={command.isPending}
          close={() => setDialog(null)}
        >
          <form
            className="space-y-4"
            onSubmit={event => {
              event.preventDefault();
              if (command.isPending || !connected) return;
              const title = String(new FormData(event.currentTarget).get('title') ?? '').trim();
              setCommandError('');
              command.mutate({ dialog, title });
            }}
          >
            {dialog.type === 'create' ? (
              <label className="block text-xs">
                Conversation title (optional)
                <input
                  data-dialog-focus
                  name="title"
                  className={field + ' mt-2'}
                  maxLength={160}
                  placeholder="New conversation"
                />
              </label>
            ) : (
              <p className={muted}>
                {dialog.type === 'delete'
                  ? `Permanently delete “${dialog.conversation.title}” and its server-saved messages and events?`
                  : `Archive “${dialog.conversation.title}”? It will remain readable under Show archived.`}
              </p>
            )}
            {commandError && <Notice error>{commandError}</Notice>}
            <div className="flex flex-wrap justify-end gap-2">
              <button
                type="button"
                className={outline}
                disabled={command.isPending}
                onClick={() => setDialog(null)}
              >
                Cancel
              </button>
              <button type="submit" className={solid} disabled={command.isPending || !connected}>
                {command.isPending
                  ? 'Working…'
                  : dialog.type === 'create'
                    ? 'Create conversation'
                    : dialog.type === 'archive'
                      ? 'Archive conversation'
                      : 'Delete permanently'}
              </button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}
