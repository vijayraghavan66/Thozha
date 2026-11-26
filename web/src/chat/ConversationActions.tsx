import { useEffect, useId, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  WebApiError,
  errorText,
  mayHaveBeenAccepted,
  type Conversation,
  type ConversationDetail,
  type WorkspaceApi,
} from '../api/client';
import { Modal, Notice } from '../components/ui';
import { muted, outline, solid } from '../components/styles';
import { conversationListKey, detailKey } from './updates';

export type ConversationAction = { type: 'archive' | 'delete'; conversation: Conversation };
export type ConversationMenuAction = 'rename' | 'archive' | 'delete';

export function ConversationConfirmation({
  action,
  api,
  connected,
  close,
  completed,
}: {
  action: ConversationAction;
  api: WorkspaceApi;
  connected: boolean;
  close: () => void;
  completed: (message: string) => void;
}) {
  const cache = useQueryClient();
  const lock = useRef(false);
  const { type, conversation } = action;
  const { id, title } = conversation;
  const archive = type === 'archive';
  const mutation = useMutation({
    mutationFn: async () => {
      try {
        if (archive) await api.archive(id);
        else {
          await api.delete(id);
          return { item: undefined, message: 'Conversation and history deleted.' };
        }
      } catch (error) {
        if (!mayHaveBeenAccepted(error) && !(error instanceof WebApiError && error.status === 404))
          throw error;

        const { items } = await api.list();
        const item = items.find(item => item.id === id);
        if (!item) return { item, message: 'Conversation is no longer on the server.' };
        if (archive && item.archived_at) return { item, message: 'Conversation archived.' };
        throw error;
      }
      const { items } = await api.list();
      const item = items.find(item => item.id === id);
      if (!item) return { item, message: 'Conversation is no longer on the server.' };
      if (!item.archived_at) throw new Error('Archive not confirmed');
      return { item, message: 'Conversation archived.' };
    },
    onSuccess: async ({ item, message }) => {
      await cache.cancelQueries({ queryKey: conversationListKey, exact: true });
      await cache.cancelQueries({ queryKey: detailKey(id), exact: true });
      cache.setQueryData<{ items: Conversation[] }>(
        conversationListKey,
        previous =>
          previous && {
            items: previous.items.flatMap(row => (row.id !== id ? [row] : item ? [item] : [])),
          }
      );
      if (!item) cache.removeQueries({ queryKey: detailKey(id), exact: true });
      else void cache.invalidateQueries({ queryKey: detailKey(id), exact: true });
      completed(message);
      void cache.invalidateQueries({ queryKey: conversationListKey, exact: true });
    },
    onSettled: () => {
      lock.current = false;
    },
  });

  return (
    <Modal
      title={archive ? 'Archive conversation' : 'Delete conversation'}
      locked={mutation.isPending}
      close={close}
    >
      <form
        className="space-y-4"
        onSubmit={event => {
          event.preventDefault();
          if (!connected || lock.current) return;
          lock.current = true;
          mutation.mutate();
        }}
      >
        <p className={muted}>
          {archive
            ? `Archive “${title}”? It will remain readable under Show archived.`
            : `Permanently delete “${title}” and its server-saved messages and events?`}
        </p>
        {mutation.isError && (
          <Notice error>
            {errorText(mutation.error, `confirm ${archive ? 'the archive' : 'the deletion'}`)}
          </Notice>
        )}
        <div className="flex flex-wrap justify-end gap-2">
          <button
            type="button"
            className={outline}
            data-dialog-focus
            disabled={mutation.isPending}
            onClick={close}
          >
            Cancel
          </button>
          <button type="submit" className={solid} disabled={mutation.isPending || !connected}>
            {mutation.isPending
              ? 'Working…'
              : archive
                ? 'Archive conversation'
                : 'Delete permanently'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function ConversationRenameDialog({
  api,
  connected,
  conversation,
  close,
  completed,
}: {
  api: WorkspaceApi;
  connected: boolean;
  conversation: Conversation;
  close: () => void;
  completed: (message: string) => void;
}) {
  const cache = useQueryClient();
  const inputId = useId();
  const lock = useRef(false);
  const [title, setTitle] = useState(conversation.title);
  const trimmedTitle = title.trim();
  const titleBytes = new TextEncoder().encode(trimmedTitle).byteLength;
  const invalid = !trimmedTitle || titleBytes > 160;
  const mutation = useMutation({
    mutationFn: () => api.rename(conversation.id, trimmedTitle),
    onSuccess: async updated => {
      await Promise.all([
        cache.cancelQueries({ queryKey: conversationListKey, exact: true }),
        cache.cancelQueries({ queryKey: detailKey(conversation.id), exact: true }),
      ]);
      cache.setQueryData<{ items: Conversation[] }>(
        conversationListKey,
        previous =>
          previous && {
            items: previous.items
              .map(item => (item.id === updated.id ? updated : item))
              .sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at)),
          }
      );
      cache.setQueryData<ConversationDetail>(detailKey(conversation.id), previous =>
        previous ? { ...previous, ...updated } : previous
      );
      completed('Conversation renamed.');
      void cache.invalidateQueries({ queryKey: conversationListKey, exact: true });
      void cache.invalidateQueries({ queryKey: detailKey(conversation.id), exact: true });
    },
    onSettled: () => {
      lock.current = false;
    },
  });

  return (
    <Modal title="Rename conversation" locked={mutation.isPending} close={close}>
      <form
        className="space-y-4"
        onSubmit={event => {
          event.preventDefault();
          if (!connected || invalid || lock.current) return;
          lock.current = true;
          mutation.mutate();
        }}
      >
        <div className="space-y-2">
          <label htmlFor={inputId} className="text-xs font-medium text-[#d7d8dd]">
            Conversation title
          </label>
          <input
            id={inputId}
            autoFocus
            value={title}
            onChange={event => {
              setTitle(event.target.value);
              if (mutation.isError) mutation.reset();
            }}
            aria-describedby={`${inputId}-help`}
            aria-invalid={invalid}
            className="w-full rounded-lg border border-white/15 bg-[#17181c] px-3 py-2.5 text-sm text-[#ececf0] outline-none focus-visible:border-white/35 focus-visible:ring-2 focus-visible:ring-white/30"
          />
          <p id={`${inputId}-help`} className={muted}>
            Titles are trimmed and limited to 160 UTF-8 bytes.
          </p>
        </div>
        {titleBytes > 160 && (
          <p role="alert" className="text-xs text-[#e6a7aa]">
            Shorten the title to 160 UTF-8 bytes or fewer.
          </p>
        )}
        {mutation.isError && (
          <Notice error>{errorText(mutation.error, 'rename the conversation')}</Notice>
        )}
        <div className="flex flex-wrap justify-end gap-2">
          <button
            type="button"
            className={outline}
            data-dialog-focus
            disabled={mutation.isPending}
            onClick={close}
          >
            Cancel
          </button>
          <button
            type="submit"
            className={solid}
            disabled={!connected || invalid || mutation.isPending}
          >
            {mutation.isPending ? 'Saving…' : 'Save title'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function ConversationHistory({
  items,
  selectedId,
  loading,
  error,
  onSelect,
  onAction,
  onNew,
  connected,
}: {
  items: Conversation[];
  selectedId: string | null;
  loading: boolean;
  error?: string;
  onSelect?: () => void;
  onAction: (
    action: ConversationMenuAction,
    conversation: Conversation,
    fallback: HTMLElement | null
  ) => void;
  onNew: () => void;
  connected: boolean;
}) {
  const [archived, setArchived] = useState(false);
  const [open, setOpen] = useState<string>();
  const [query, setQuery] = useState('');
  const historyToggle = useRef<HTMLButtonElement>(null);
  const actionsId = useId();
  const restoreNoteId = `${actionsId}-restore-note`;
  const rows = items.filter(item => Boolean(item.archived_at) === archived);
  const matchingRows = rows.filter(item =>
    item.title.toLowerCase().includes(query.trim().toLowerCase())
  );
  return (
    <section className="th-v4-history" aria-label="Conversation history">
      <p className="th-v4-eyebrow">{archived ? 'ARCHIVED CONVERSATIONS' : 'SAVED CONVERSATIONS'}</p>
      <label className="sr-only" htmlFor={`${actionsId}-search`}>
        Search conversations
      </label>
      <input
        id={`${actionsId}-search`}
        type="search"
        className="th-history-search"
        placeholder="Search conversations"
        value={query}
        onChange={event => setQuery(event.target.value)}
      />
      <button type="button" className="th-v4-history-new" onClick={onNew}>
        <span aria-hidden="true">＋</span>
        <span>New conversation</span>
      </button>
      <button
        ref={historyToggle}
        type="button"
        className="th-history-filter"
        onClick={() => {
          setOpen(undefined);
          setArchived(!archived);
        }}
      >
        {archived ? 'Show active conversations' : 'Show archived conversations'}
      </button>
      {archived && (
        <p id={restoreNoteId} className="th-history-restore-note">
          Restore is unavailable because Web v1 has no restore operation.
        </p>
      )}
      {loading && <p className={muted}>Loading history…</p>}
      {error && (
        <p role="alert" className={muted}>
          {error}
        </p>
      )}
      {connected && !loading && !error && !matchingRows.length && (
        <p className={muted}>
          {query.trim()
            ? 'No matching conversations.'
            : archived
              ? 'No archived conversations.'
              : 'No conversations yet.'}
        </p>
      )}
      <ul className="th-history-rows">
        {matchingRows.map(item => (
          <ConversationPill
            key={item.id}
            panelId={`${actionsId}-${item.id}`}
            restoreNoteId={restoreNoteId}
            item={item}
            selected={selectedId === item.id}
            expanded={open === item.id}
            connected={connected}
            setOpen={setOpen}
            onSelect={onSelect}
            onAction={type => onAction(type, item, historyToggle.current)}
          />
        ))}
      </ul>
    </section>
  );
}

function ConversationPill({
  panelId,
  restoreNoteId,
  item,
  selected,
  expanded,
  connected,
  setOpen,
  onSelect,
  onAction,
}: {
  panelId: string;
  restoreNoteId: string;
  item: Conversation;
  selected: boolean;
  expanded: boolean;
  connected: boolean;
  setOpen: React.Dispatch<React.SetStateAction<string | undefined>>;
  onSelect?: () => void;
  onAction: (type: ConversationMenuAction) => void;
}) {
  const row = useRef<HTMLLIElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const actions = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!expanded) return;
    const frame = requestAnimationFrame(() => {
      const first = actions.current?.querySelector<HTMLButtonElement>(
        '[role="menuitem"]:not(:disabled)'
      );
      first?.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  }, [expanded]);
  useEffect(() => {
    if (!expanded) return;
    const outside = (event: PointerEvent) => {
      if (row.current && !event.composedPath().includes(row.current)) {
        if (row.current.contains(document.activeElement))
          trigger.current?.focus({ preventScroll: true });
        setOpen(current => (current === item.id ? undefined : current));
      }
    };
    document.addEventListener('pointerdown', outside, true);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
    };
  }, [expanded, item.id, setOpen]);

  function close() {
    trigger.current?.focus({ preventScroll: true });
    setOpen(undefined);
  }

  return (
    <li
      ref={row}
      className="th-history-row"
      data-actions={expanded}
      data-selected={selected}
      onBlur={event => {
        if (expanded && !event.currentTarget.contains(event.relatedTarget))
          setOpen(current => (current === item.id ? undefined : current));
      }}
      onKeyDown={event => {
        if (!expanded) return;
        if (event.key === 'Escape') {
          event.preventDefault();
          event.stopPropagation();
          close();
          return;
        }
        if (event.key === 'Tab') {
          setOpen(current => (current === item.id ? undefined : current));
          return;
        }
        if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key))
          return;
        const items = Array.from(
          actions.current?.querySelectorAll<HTMLButtonElement>(
            '[role="menuitem"]:not(:disabled)'
          ) ?? []
        );
        if (!items.length) return;
        const current = items.indexOf(document.activeElement as HTMLButtonElement);
        const next =
          event.key === 'Home'
            ? 0
            : event.key === 'End'
              ? items.length - 1
              : (current +
                  (event.key === 'ArrowDown' || event.key === 'ArrowRight' ? 1 : -1) +
                  items.length) %
                items.length;
        event.preventDefault();
        items[next].focus();
      }}
    >
      <a
        href={`#/ai-workspace?conversation=${encodeURIComponent(item.id)}${item.archived_at ? '&archive=1' : ''}`}
        className="th-v4-history-link"
        title={item.title}
        aria-current={selected ? 'page' : undefined}
        onClick={() => {
          setOpen(undefined);
          onSelect?.();
        }}
      >
        {item.title}
      </a>
      <div
        ref={actions}
        id={panelId}
        className="th-history-actions"
        role="menu"
        aria-label={`Actions for ${item.title}`}
        aria-hidden={!expanded}
        inert={!expanded}
      >
        <button
          type="button"
          role="menuitem"
          tabIndex={-1}
          disabled={!connected}
          onClick={() => {
            close();
            onAction('rename');
          }}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path d="m3 11.5-.75 2.25 2.4-.7L12.8 4.9a1.8 1.8 0 0 0-2.55-2.55L3 9.6z M8.9 3.7l3.4 3.4" />
          </svg>
          <span>Rename</span>
        </button>
        {!item.archived_at && (
          <button
            type="button"
            aria-label="Archive"
            role="menuitem"
            tabIndex={-1}
            disabled={!connected}
            onClick={() => {
              close();
              onAction('archive');
            }}
          >
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M2 3h12v3H2z M3 6v7h10V6 M6.5 9h3" />
            </svg>
            <span>Archive</span>
          </button>
        )}
        {item.archived_at && (
          <button
            type="button"
            role="menuitem"
            tabIndex={-1}
            aria-disabled="true"
            aria-describedby={restoreNoteId}
            title="Restore is unavailable because Web v1 has no restore operation."
            className="th-history-restore-action"
            onClick={event => event.preventDefault()}
          >
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M3 5.5V2.75m0 2.75h2.75M3 5.5a5 5 0 1 1-1 4.5" />
            </svg>
            <span>Restore</span>
          </button>
        )}
        <button
          type="button"
          aria-label="Delete"
          role="menuitem"
          tabIndex={-1}
          className="th-history-delete"
          disabled={!connected}
          onClick={() => {
            close();
            onAction('delete');
          }}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path d="M2.5 4.5h11 M6 4.5V2.75h4V4.5 M4 4.5l.75 8.75h6.5L12 4.5 M7 7v3.5 M9 7v3.5" />
          </svg>
          <span>Delete</span>
        </button>
      </div>
      <button
        ref={trigger}
        type="button"
        className="th-history-more"
        title={expanded ? 'Close actions' : 'Conversation actions'}
        aria-label={`${expanded ? 'Close actions for' : 'Actions for'} ${item.title}`}
        aria-haspopup="menu"
        aria-expanded={expanded}
        aria-controls={panelId}
        onKeyDown={event => {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          if (expanded) close();
          else setOpen(item.id);
        }}
        onClick={() => (expanded ? close() : setOpen(item.id))}
      >
        <svg
          className="th-history-dots"
          viewBox="0 0 16 16"
          width="16"
          height="16"
          fill="currentColor"
          aria-hidden="true"
        >
          <circle cx="3" cy="8" r="1.25" />
          <circle cx="8" cy="8" r="1.25" />
          <circle cx="13" cy="8" r="1.25" />
        </svg>
        <svg
          className="th-history-close"
          viewBox="0 0 16 16"
          width="16"
          height="16"
          fill="none"
          stroke="currentColor"
          strokeLinecap="round"
          strokeWidth="1.3"
          aria-hidden="true"
        >
          <path d="m4.5 4.5 7 7m0-7-7 7" />
        </svg>
      </button>
    </li>
  );
}
