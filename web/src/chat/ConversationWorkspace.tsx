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
  type WebAttachmentMediaType,
  type WebAttachmentMetadata,
  type WebMessage,
  type WorkspaceApi,
} from '../api/client';
import type { WebConversationStreamMode } from '../../../contracts/web-api/v1';
import { Chip, Loading, Notice } from '../components/ui';
import { formatDate, muted, outline } from '../components/styles';
import { MarkdownMessage } from './MarkdownMessage';
import { LivingPulseMark } from '../auth/LivingPulseStartup';
import { SilverIntelligence } from '../components/SilverIntelligence';
import { WorkspaceIcon } from '../components/WorkspaceIcon';
import {
  motionAllowed,
  motionEase,
  motionTiming,
  resolveIdentity,
  useSceneArrival,
  type IdentityState,
} from '../components/motion';
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
type AttachmentDraft = {
  key: string;
  file: File;
  mediaType: WebAttachmentMediaType;
  status: 'ready' | 'uploading' | 'uploaded' | 'failed' | 'interrupted';
  progress?: number;
  metadata?: WebAttachmentMetadata;
  error?: string;
  canRemove?: boolean;
};
type Attempt = { id: string; key: string; text: string; attachments: AttachmentDraft[] };
type SendIssue = { attempt: Attempt; message: string; uncertain: boolean };
type AttachmentFeedback = { message: string; error: boolean };
const maxAttachmentSize = 8 * 1024 * 1024;
const maxConversationAttachmentBytes = 30 * 1024 * 1024;
const maxMessageAttachments = 5;
const maxConversationAttachments = 10;

class AttachmentUploadError extends Error {
  constructor(readonly original: unknown) {
    super('Attachment upload failed');
    this.name = 'AttachmentUploadError';
  }
}

function mediaTypeFor(file: File): WebAttachmentMediaType | undefined {
  if (file.type === 'image/png' || file.type === 'image/jpeg' || file.type === 'application/pdf')
    return file.type;
  if (file.type) return undefined;
  const extension = file.name.toLowerCase().split('.').pop();
  if (extension === 'png') return 'image/png';
  if (extension === 'jpg' || extension === 'jpeg') return 'image/jpeg';
  if (extension === 'pdf') return 'application/pdf';
  return undefined;
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB'];
  let value = bytes / 1024;
  let unit = units[0];
  if (value >= 1024) {
    value /= 1024;
    unit = units[1];
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${unit}`;
}

function attachmentUploadError(error: unknown) {
  if (error instanceof WebApiError && error.detail) return error.detail;
  return errorText(error, 'upload the attachment');
}

function AttachmentPreview({ file, mediaType }: { file: File; mediaType: WebAttachmentMediaType }) {
  const [url, setUrl] = useState<string>();
  useEffect(() => {
    if (mediaType === 'application/pdf') return;
    const next = URL.createObjectURL(file);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [file, mediaType]);
  if (mediaType !== 'application/pdf' && url)
    return <img src={url} alt="" className="h-9 w-9 shrink-0 rounded-md object-cover" />;
  return (
    <span
      aria-hidden="true"
      className="grid h-9 w-9 shrink-0 place-items-center rounded-md border border-white/10 bg-white/[.04] text-[10px] font-medium text-[#c4c5cb]"
    >
      {mediaType === 'application/pdf' ? 'PDF' : 'IMG'}
    </span>
  );
}

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
  ended?: boolean;
};

function Message({
  message,
  cancellation,
  reuse,
  live,
  attachments,
}: {
  message: WebMessage;
  cancellation: boolean;
  reuse?: () => void;
  live?: LiveResponse;
  attachments?: WebAttachmentMetadata[];
}) {
  if (message.role === 'user') {
    return (
      <div
        data-message-id={message.id}
        className="th-v4-user-message ml-auto max-w-[85%] rounded-2xl rounded-br-sm border border-white/15 bg-[#2b2c32] px-4 py-3 text-[13px] text-[#f1f1f3]"
      >
        <span className="sr-only">You: </span>
        <p className="whitespace-pre-wrap break-words">{message.content}</p>
        {attachments?.length ? (
          <ul aria-label="Attached files" className="mt-3 space-y-2 border-t border-white/10 pt-2">
            {attachments.map(attachment => (
              <li key={attachment.id} className="flex min-w-0 items-start justify-between gap-3">
                <span className="min-w-0 break-all text-[11px] font-medium">
                  {attachment.file_name}
                </span>
                <span className="shrink-0 text-[10px] text-[#c4c5cb]">
                  {attachment.media_type === 'application/pdf'
                    ? `${attachment.page_count} page${attachment.page_count === 1 ? '' : 's'}`
                    : attachment.media_type === 'image/png'
                      ? 'PNG'
                      : 'JPEG'}{' '}
                  · {formatBytes(attachment.size_bytes)}
                </span>
              </li>
            ))}
          </ul>
        ) : message.attachment_ids?.length ? (
          <p className="mt-2 text-[10px] text-[#c4c5cb]">Saved attachment metadata unavailable.</p>
        ) : null}
      </div>
    );
  }
  return (
    <div
      data-message-id={message.id}
      data-response-state={message.status}
      className="th-v4-assistant-message max-w-[95%] text-[13px] leading-7 [content-visibility:auto] [contain-intrinsic-size:auto_64px]"
    >
      <div className="th-message-identity mb-2 text-[10px] font-bold tracking-widest text-[#aaacb5]">
        <LivingPulseMark
          state={
            isPendingResponse(message.status)
              ? 'thinking'
              : message.status === 'failed'
                ? 'failed'
                : message.status === 'cancelled'
                  ? 'cancelled'
                  : 'completed'
          }
        />
        THOZHA
      </div>
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
  onActivityChange,
  workspaceIdentity,
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
  onActivityChange: (state: IdentityState) => void;
  workspaceIdentity: IdentityState;
}) {
  const cache = useQueryClient();
  const [selection, setSelection] = useState<string | null>(null);
  const [archived, setArchived] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [attachmentDrafts, setAttachmentDrafts] = useState<Record<string, AttachmentDraft[]>>({});
  const [attachmentFeedback, setAttachmentFeedback] = useState<AttachmentFeedback>();
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
  const composerResize = useRef<Animation | null>(null);
  const attachmentInput = useRef<HTMLInputElement>(null);
  const composerOrigin = useRef<number | undefined>(undefined);
  const focusDraft = useRef(false);
  const createLock = useRef(false);
  const createUncertain = useRef(false);
  const followLatest = useRef(true);
  const previousConversation = useRef<string | null>(null);
  const scrollPositions = useRef(new Map<string, number>());
  const followLatestByConversation = useRef(new Map<string, boolean>());
  const pendingScrollRestore = useRef<{ id: string | null; top: number } | null>(null);
  const seenMessages = useRef<{ id: string | null; ids: Set<string> }>({
    id: null,
    ids: new Set(),
  });
  const activeSend = useRef(new Set<string>());
  const activeStream = useRef<AbortController | null>(null);
  const fieldId = useId();
  const enabled = active && connected;
  useEffect(() => () => activeStream.current?.abort(), []);
  useEffect(() => () => composerResize.current?.cancel(), []);
  useEffect(() => {
    if (!changedConversationId) return;
    setSelection(previous => (previous === changedConversationId ? null : previous));
    setDrafts(previous => omitConversation(previous, changedConversationId));
    setAttachmentDrafts(previous => omitConversation(previous, changedConversationId));
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
    live?.conversationId === id && !live.ended
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
  const attachmentDraftKey = id ?? '__new__';
  const selectedAttachments = attachmentDrafts[attachmentDraftKey] ?? [];
  useEffect(() => {
    if (active && welcome && selectedAttachments.length) {
      const container = log.current?.parentElement;
      if (container) container.scrollTop = container.scrollHeight;
    }
  }, [active, welcome, selectedAttachments.length]);
  const savedAttachments = messages?.flatMap(message => message.attachments ?? []) ?? [];
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
    const textarea = composer.current;
    if (!active || !textarea) return;
    function resize() {
      if (!textarea) return;
      const before = textarea.getBoundingClientRect().height;
      composerResize.current?.cancel();
      textarea.style.height = 'auto';
      const height = Math.min(160, textarea.scrollHeight);
      textarea.style.height = `${height}px`;
      if (!reduced && textarea.animate && motionAllowed(textarea) && Math.abs(before - height) > 1)
        composerResize.current = textarea.animate(
          [{ height: `${before}px` }, { height: `${height}px` }],
          { duration: motionTiming.control, easing: motionEase }
        );
    }
    resize();
    let width = textarea.getBoundingClientRect().width;
    const observer = new ResizeObserver(() => {
      const next = textarea.getBoundingClientRect().width;
      if (next === width) return;
      width = next;
      resize();
    });
    observer.observe(textarea, { box: 'border-box' });
    return () => observer.disconnect();
  }, [active, draft, id, writingNew, reduced]);
  const generatingMessage = messages?.find(
    message => message.role === 'assistant' && isPendingResponse(message.status)
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
    if (switched) {
      previousConversation.current = id;
      const key = id ?? '__new__';
      followLatest.current = followLatestByConversation.current.get(key) ?? true;
      const savedTop = scrollPositions.current.get(key);
      pendingScrollRestore.current = savedTop === undefined ? null : { id, top: savedTop };
    }
    if (pendingScrollRestore.current?.id === id) {
      if (detail.isPending) return;
      log.current.scrollTop = pendingScrollRestore.current.top;
      pendingScrollRestore.current = null;
      return;
    }
    if (followLatest.current)
      log.current.scrollTo({
        top: log.current.scrollHeight,
        behavior:
          switched ||
          live?.partial ||
          reduced ||
          matchMedia('(prefers-reduced-motion: reduce)').matches
            ? 'instant'
            : 'smooth',
      });
  }, [active, awaitingHistory, detail.isPending, id, live?.partial, messages, reduced]);

  useSceneArrival(log, id, active && !reduced);
  useLayoutEffect(() => {
    if (!messages) return;
    const previous = seenMessages.current;
    const arrivals = previous.id === id ? messages.filter(item => !previous.ids.has(item.id)) : [];
    seenMessages.current = { id, ids: new Set(messages.map(item => item.id)) };
    if (!active || reduced || !motionAllowed(log.current)) return;
    const animations: Animation[] = [];
    for (const message of arrivals) {
      const node = Array.from(
        log.current?.querySelectorAll<HTMLElement>('[data-message-id]') ?? []
      ).find(element => element.dataset.messageId === message.id);
      if (!node?.animate) continue;
      animations.push(
        node.animate(
          [
            {
              opacity: 0,
              translate: message.role === 'user' ? '18px 26px' : '0 20px',
              scale: message.role === 'user' ? '.95' : '.99',
            },
            { opacity: 1, translate: '0 0', scale: '1' },
          ],
          { duration: motionTiming.panel, easing: motionEase }
        )
      );
    }
    return () => animations.forEach(animation => animation.cancel());
  }, [active, id, messages, reduced]);

  const command = useMutation({
    mutationFn: ({ text }: { text: string; attachments: AttachmentDraft[] }) =>
      api.create({ title: titleFromMessage(text) }),
    onSuccess: async (created, input) => {
      await cache.cancelQueries({ queryKey: listKey });
      if (active) measureComposerOrigin();
      cache.setQueryData<{ items: Conversation[] }>(listKey, initial => ({
        items: [created, ...(initial?.items ?? []).filter(item => item.id !== created.id)],
      }));
      setDrafts(previous => ({ ...previous, [created.id]: input.text }));
      setAttachmentDrafts(previous => {
        const next = { ...previous };
        delete next.__new__;
        if (input.attachments.length) next[created.id] = input.attachments;
        return next;
      });
      setFirstDraft('');
      setFresh(false);
      setArchived(false);
      setSelection(created.id);
      if (window.location.hash.startsWith('#/ai-workspace') || !window.location.hash)
        window.location.hash = `#/ai-workspace?conversation=${encodeURIComponent(created.id)}`;
      beginSend({
        id: created.id,
        key: crypto.randomUUID(),
        text: input.text,
        attachments: input.attachments,
      });
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
    setAttachmentDrafts(previous => omitConversation(previous, attempt.id));
    setAttachmentFeedback(undefined);
    void cache.invalidateQueries({ queryKey: detailKey(attempt.id) });
    void cache.invalidateQueries({ queryKey: listKey });
  }

  function updateAttachmentDraft(
    conversationId: string,
    attachmentKey: string,
    patch: Partial<AttachmentDraft>
  ) {
    setAttachmentDrafts(previous => ({
      ...previous,
      [conversationId]: (previous[conversationId] ?? []).map(attachment =>
        attachment.key === attachmentKey ? { ...attachment, ...patch } : attachment
      ),
    }));
  }

  const send = useMutation({
    mutationFn: async ({ attempt, recovery }: { attempt: Attempt; recovery: boolean }) => {
      const controller = new AbortController();
      activeStream.current = controller;
      try {
        const attachmentIds: string[] = [];
        for (const attachment of attempt.attachments) {
          if (attachment.metadata) {
            attachmentIds.push(attachment.metadata.id);
            continue;
          }
          setAttachmentFeedback(undefined);
          updateAttachmentDraft(attempt.id, attachment.key, {
            status: 'uploading',
            progress: undefined,
            error: undefined,
          });
          try {
            const uploaded = await api.uploadAttachment(
              attempt.id,
              attachment.file,
              attachment.mediaType,
              attachment.key,
              controller.signal,
              (sent, total) =>
                updateAttachmentDraft(attempt.id, attachment.key, {
                  progress: Math.min(100, Math.round((sent / total) * 100)),
                })
            );
            attachmentIds.push(uploaded.attachment.id);
            updateAttachmentDraft(attempt.id, attachment.key, {
              status: 'uploaded',
              progress: 100,
              metadata: uploaded.attachment,
              error: undefined,
              canRemove: false,
            });
          } catch (error) {
            const interrupted =
              controller.signal.aborted ||
              (error instanceof DOMException && error.name === 'AbortError');
            const status = error instanceof WebApiError ? error.status : undefined;
            const canRemove = [413, 415, 422, 429, 503].includes(status ?? 0);
            const message = interrupted
              ? 'Upload interrupted. Retrying uses the same idempotency key; the host may have received it.'
              : attachmentUploadError(error);
            updateAttachmentDraft(attempt.id, attachment.key, {
              status: interrupted ? 'interrupted' : 'failed',
              canRemove,
              error: message,
            });
            setAttachmentFeedback({
              message: `Could not upload ${attachment.file.name}: ${message}`,
              error: true,
            });
            throw new AttachmentUploadError(error);
          }
        }
        const body = {
          content: attempt.text,
          ...(attachmentIds.length ? { attachment_ids: attachmentIds } : {}),
        };
        if (!recovery) {
          let acknowledged = false;
          let responseId: string | undefined;
          let chunks: string[] = [];
          let frame: number | undefined;
          function flush() {
            frame = undefined;
            if (!chunks.length) return;
            const text = chunks.join('');
            chunks = [];
            setLive(current =>
              current && current.responseId === responseId && current.mode === 'engine_stream'
                ? { ...current, partial: current.partial + text }
                : current
            );
          }
          try {
            await api.stream(attempt.id, body, attempt.key, controller.signal, event => {
              switch (event.event) {
                case 'accepted':
                  acknowledged = true;
                  responseId = event.data.response_id;
                  setLive({
                    conversationId: attempt.id,
                    responseId: event.data.response_id,
                    partial: '',
                  });
                  recordAcceptance(attempt, event.data.response_id);
                  break;
                case 'stream_mode':
                  setLive(current =>
                    current?.responseId === event.data.response_id
                      ? { ...current, mode: event.data.mode }
                      : current
                  );
                  break;
                case 'delta':
                  if (event.data.response_id === responseId) {
                    chunks.push(event.data.content);
                    frame ??= requestAnimationFrame(flush);
                  }
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
            if (frame !== undefined) cancelAnimationFrame(frame);
            if (!controller.signal.aborted) flush();
            setLive(current =>
              current && current.responseId === responseId ? { ...current, ended: true } : current
            );
          }
        }
        const accepted = await api.submit(attempt.id, body, attempt.key);
        recordAcceptance(attempt, accepted.response_id);
      } finally {
        if (activeStream.current === controller) activeStream.current = null;
      }
    },
    onError: (error, { attempt }) => {
      if (error instanceof AttachmentUploadError) return;
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
  const lastAssistant = messages?.filter(message => message.role === 'assistant').at(-1);
  const activity: IdentityState =
    sending || pending || awaitingHistory
      ? 'thinking'
      : issue || lastAssistant?.status === 'failed'
        ? 'failed'
        : live?.conversationId === id && live.ended && lastAssistant?.id === live.responseId
          ? lastAssistant.status === 'cancelled'
            ? 'cancelled'
            : 'completed'
          : 'idle';
  const identity = resolveIdentity(activity, workspaceIdentity);
  const identityVisible = welcome || identity !== 'idle';
  useEffect(() => {
    if (active) onActivityChange(activity);
  }, [active, activity, onActivityChange]);
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
    setAttachmentFeedback(undefined);
    send.mutate({ attempt, recovery });
  }

  function measureComposerOrigin() {
    composerOrigin.current = composer.current?.form?.getBoundingClientRect().top;
  }

  function addFiles(files: FileList | null) {
    if (!files) return;
    setAttachmentFeedback(undefined);
    const staged = attachmentDrafts[attachmentDraftKey] ?? [];
    const savedCount = savedAttachments.length;
    const savedBytes = savedAttachments.reduce(
      (total, attachment) => total + attachment.size_bytes,
      0
    );
    let stagedBytes = staged.reduce((total, attachment) => total + attachment.file.size, 0);
    const accepted: AttachmentDraft[] = [];
    const errors: string[] = [];
    for (const file of Array.from(files)) {
      const mediaType = mediaTypeFor(file);
      if (!mediaType) {
        errors.push(`${file.name}: choose a PNG, JPEG, or PDF file.`);
        continue;
      }
      if (file.size === 0 || file.size > maxAttachmentSize) {
        errors.push(`${file.name}: files must be between 1 byte and 8 MiB.`);
        continue;
      }
      if (staged.length + accepted.length >= maxMessageAttachments) {
        errors.push(`A message can include at most ${maxMessageAttachments} files.`);
        break;
      }
      if (savedCount + staged.length + accepted.length >= maxConversationAttachments) {
        errors.push(`A conversation can include at most ${maxConversationAttachments} files.`);
        break;
      }
      if (savedBytes + stagedBytes + file.size > maxConversationAttachmentBytes) {
        errors.push('The conversation attachment limit is 30 MiB.');
        continue;
      }
      const duplicate = [...staged, ...accepted].some(
        attachment =>
          attachment.file.name === file.name &&
          attachment.file.size === file.size &&
          attachment.file.lastModified === file.lastModified
      );
      if (duplicate) {
        errors.push(`${file.name}: this file is already staged.`);
        continue;
      }
      accepted.push({
        key: crypto.randomUUID(),
        file,
        mediaType,
        status: 'ready',
        canRemove: true,
      });
      stagedBytes += file.size;
    }
    if (accepted.length)
      setAttachmentDrafts(previous => ({
        ...previous,
        [attachmentDraftKey]: [...(previous[attachmentDraftKey] ?? []), ...accepted],
      }));
    if (errors.length) setAttachmentFeedback({ message: errors.join(' '), error: true });
  }

  function removeAttachment(attachmentKey: string) {
    setAttachmentDrafts(previous => {
      const remaining = (previous[attachmentDraftKey] ?? []).filter(
        attachment => attachment.key !== attachmentKey
      );
      return remaining.length
        ? { ...previous, [attachmentDraftKey]: remaining }
        : omitConversation(previous, attachmentDraftKey);
    });
    setAttachmentFeedback(undefined);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = draft.trim();
    if (composerDisabled || !value || createLock.current) return;
    setPaletteDismissed(true);
    if (slash) {
      if (selectedAttachments.length) {
        setAttachmentFeedback({
          message: 'Send attachments with a normal conversation message, not a slash command.',
          error: true,
        });
        return;
      }
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
    if (id)
      beginSend({
        id,
        key: crypto.randomUUID(),
        text: value,
        attachments: selectedAttachments,
      });
    else if (writingNew && !createUncertain.current && !activeSend.current.size) {
      createLock.current = true;
      setSlashError('');
      command.mutate({ text: value, attachments: selectedAttachments });
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
    setAttachmentDrafts(previous => omitConversation(previous, '__new__'));
    setAttachmentFeedback(undefined);
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
          data-activity={activity}
          className={`th-v4-conversation flex min-h-0 min-w-0 flex-col overflow-hidden ${welcome ? 'is-welcome' : ''} ${selectedAttachments.length ? 'has-attachments' : ''}`}
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
              const isFollowing = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
              const key = id ?? '__new__';
              scrollPositions.current.set(key, el.scrollTop);
              followLatest.current = isFollowing;
              followLatestByConversation.current.set(key, isFollowing);
            }}
            className="th-v4-message-log flex min-h-[230px] flex-1 flex-col gap-7 overflow-y-auto p-5"
          >
            <div
              className="th-intelligence-stage"
              data-mode={welcome ? 'hero' : identityVisible ? 'presence' : 'hidden'}
            >
              <SilverIntelligence
                state={identity}
                active={active && identityVisible}
                mode={welcome ? 'hero' : identityVisible ? 'presence' : 'hidden'}
              />
              {!welcome && identityVisible && (
                <span className="th-presence-label" aria-hidden="true">
                  {
                    {
                      idle: '',
                      thinking: 'Thinking',
                      executing: 'Supervisor task in progress',
                      approval: 'Waiting for your approval',
                      completed: 'Response complete',
                      failed: 'Needs attention',
                      cancelled: 'Cancelled',
                    }[identity]
                  }
                </span>
              )}
            </div>
            {welcome && (
              <div className="th-v4-empty th-v4-welcome text-center">
                <span className="th-welcome-eyebrow">THOZHA</span>
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
                  attachments={message.attachments}
                  live={live?.responseId === message.id ? live : undefined}
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
                    onClick={() =>
                      beginSend(
                        {
                          ...issue.attempt,
                          attachments:
                            attachmentDrafts[issue.attempt.id] ?? issue.attempt.attachments,
                        },
                        true
                      )
                    }
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
            <form data-intro-composer onSubmit={submit} className="th-chat-composer">
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
              <input
                ref={attachmentInput}
                type="file"
                accept="image/png,image/jpeg,application/pdf,.png,.jpg,.jpeg,.pdf"
                multiple
                className="sr-only"
                aria-label="Choose PNG, JPEG, or PDF attachments"
                onChange={event => {
                  addFiles(event.currentTarget.files);
                  event.currentTarget.value = '';
                }}
              />
              {attachmentFeedback && (
                <div className="px-2 pt-2">
                  <Notice error={attachmentFeedback.error}>{attachmentFeedback.message}</Notice>
                </div>
              )}
              {selectedAttachments.length > 0 && (
                <ul aria-label="Attachments to send" className="th-composer-attachments">
                  {selectedAttachments.map(attachment => (
                    <li key={attachment.key} className="th-composer-attachment">
                      <AttachmentPreview file={attachment.file} mediaType={attachment.mediaType} />
                      <div className="min-w-0 flex-1">
                        <p
                          className="truncate text-[11px] font-medium"
                          title={attachment.file.name}
                        >
                          {attachment.metadata?.file_name ?? attachment.file.name}
                        </p>
                        <p
                          role={attachment.status === 'uploading' ? 'status' : undefined}
                          className={`mt-1 truncate text-[10px] ${attachment.error ? 'text-[#e1c3c3]' : muted}`}
                        >
                          {attachment.error ??
                            (attachment.status === 'uploading'
                              ? attachment.progress === undefined
                                ? 'Uploading to the Thozha host…'
                                : attachment.progress === 100
                                  ? 'Transfer complete · Processing on host…'
                                  : `Uploading · ${attachment.progress}%`
                              : attachment.status === 'uploaded'
                                ? `Uploaded · ${formatBytes(attachment.metadata?.size_bytes ?? attachment.file.size)}`
                                : attachment.status === 'interrupted'
                                  ? 'Upload interrupted. Retry uses the same upload key.'
                                  : `${attachment.mediaType.replace('application/', '').replace('image/', '').toUpperCase()} · ${formatBytes(attachment.file.size)}`)}
                        </p>
                        {attachment.status === 'uploading' && attachment.progress !== undefined && (
                          <progress
                            className="mt-1 h-1 w-full accent-[#c4c5cb]"
                            value={attachment.progress}
                            max={100}
                            aria-label={`Upload progress for ${attachment.file.name}`}
                          />
                        )}
                      </div>
                      {attachment.status === 'uploading' ? (
                        <button
                          type="button"
                          className="th-attachment-remove"
                          onClick={() => activeStream.current?.abort()}
                        >
                          Cancel upload
                        </button>
                      ) : (attachment.status === 'ready' || attachment.canRemove) && !sending ? (
                        <button
                          type="button"
                          className="th-attachment-remove"
                          aria-label={`Remove ${attachment.file.name}`}
                          onClick={() => removeAttachment(attachment.key)}
                        >
                          <span aria-hidden="true">×</span>
                        </button>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
              <textarea
                ref={composer}
                rows={1}
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
                className="th-composer-input"
              />
              <div className="th-composer-toolbar">
                <button
                  type="button"
                  className="th-composer-attach"
                  aria-label="Attach file"
                  title="Attach PNG, JPEG, or PDF · up to 8 MiB each"
                  disabled={composerDisabled}
                  onClick={() => attachmentInput.current?.click()}
                >
                  <WorkspaceIcon name="attach" />
                </button>
                <span id={fieldId} className="sr-only">
                  PNG, JPEG, or PDF · 8 MiB each · up to 5 per message . Enter to send. Shift+Enter
                  for a new line. Slash for commands.
                </span>
                <details className="th-composer-help">
                  <summary aria-label="Message help" title="Attachments and keyboard shortcuts">
                    ?
                  </summary>
                  <div>
                    <p>Enter to send · Shift+Enter for a new line · / for commands</p>
                    <p>PNG, JPEG, or PDF · 8 MiB each · up to 5 per message.</p>
                    <p>
                      The host extracts printed text. Diagrams and handwriting may not be readable.
                    </p>
                  </div>
                </details>
                <span className="th-composer-hint">
                  {paletteOpen
                    ? '↑↓ select · Tab complete · Esc close'
                    : pending
                      ? 'Generating…'
                      : sending
                        ? 'Sending…'
                        : 'Enter to send'}
                </span>
                {generatingMessage && id ? (
                  <button
                    className="th-composer-send"
                    type="button"
                    disabled={
                      !connected ||
                      !connected ||
                      cancel.isPending ||
                      pendingCancellation.has(generatingMessage.id)
                    }
                    onClick={() =>
                      cancel.mutate({ conversationId: id, responseId: generatingMessage.id })
                    }
                    aria-label="Stop generating"
                    title="Request cancellation"
                  >
                    <WorkspaceIcon name="stop" />
                  </button>
                ) : (
                  <button
                    disabled={
                      composerDisabled ||
                      !draft.trim() ||
                      (writingNew && createUncertain.current && !slash)
                    }
                    className="th-composer-send"
                    type="submit"
                    aria-label={
                      sending || command.isPending
                        ? 'Sending…'
                        : slash?.command?.name === 'task'
                          ? 'Review task'
                          : slash
                            ? 'Run command'
                            : 'Send ↑'
                    }
                    title={
                      slash?.command?.name === 'task'
                        ? 'Review task'
                        : slash
                          ? 'Run command'
                          : 'Send message'
                    }
                  >
                    <WorkspaceIcon name="send" />
                  </button>
                )}
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
