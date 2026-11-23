import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  WebApiError,
  errorText,
  mayHaveBeenAccepted,
  type Conversation,
  type WorkspaceApi,
} from '../api/client';
import { Modal, Notice } from '../components/ui';
import { muted, outline, solid } from '../components/styles';
import { conversationListKey, detailKey } from './updates';

export type ConversationAction = { type: 'archive' | 'delete'; conversation: Conversation };

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

export function ConversationHistory({
  items,
  selectedId,
  loading,
  error,
  onSelect,
  onAction,
  connected,
}: {
  items: Conversation[];
  selectedId: string | null;
  loading: boolean;
  error?: string;
  onSelect?: () => void;
  onAction: (action: ConversationAction, fallback: HTMLElement | null) => void;
  connected: boolean;
}) {
  const [archived, setArchived] = useState(false);
  const [open, setOpen] = useState<string>();
  const historyToggle = useRef<HTMLButtonElement>(null);
  const actionsId = useId();
  const rows = items.filter(item => Boolean(item.archived_at) === archived);
  return (
    <section className="th-v4-history" aria-label="Conversation history">
      <p className="th-v4-eyebrow">{archived ? 'ARCHIVED CONVERSATIONS' : 'SAVED CONVERSATIONS'}</p>
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
      {loading && <p className={muted}>Loading history…</p>}
      {error && (
        <p role="alert" className={muted}>
          {error}
        </p>
      )}
      {connected && !loading && !error && !rows.length && (
        <p className={muted}>{archived ? 'No archived conversations.' : 'No conversations yet.'}</p>
      )}
      <ul className="th-history-rows">
        {rows.map(item => (
          <ConversationPill
            key={item.id}
            panelId={`${actionsId}-${item.id}`}
            item={item}
            selected={selectedId === item.id}
            expanded={open === item.id}
            connected={connected}
            setOpen={setOpen}
            onSelect={onSelect}
            onAction={type => onAction({ type, conversation: item }, historyToggle.current)}
          />
        ))}
      </ul>
    </section>
  );
}

function ConversationPill({
  panelId,
  item,
  selected,
  expanded,
  connected,
  setOpen,
  onSelect,
  onAction,
}: {
  panelId: string;
  item: Conversation;
  selected: boolean;
  expanded: boolean;
  connected: boolean;
  setOpen: React.Dispatch<React.SetStateAction<string | undefined>>;
  onSelect?: () => void;
  onAction: (type: ConversationAction['type']) => void;
}) {
  const row = useRef<HTMLLIElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const actions = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (expanded)
      actions.current
        ?.querySelector<HTMLButtonElement>('button:not(:disabled)')
        ?.focus({ preventScroll: true });
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
    return () => document.removeEventListener('pointerdown', outside, true);
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
        if (!expanded || event.key !== 'Escape') return;
        event.preventDefault();
        event.stopPropagation();
        close();
      }}
    >
      <a
        href={`#/ai-workspace?conversation=${encodeURIComponent(item.id)}${item.archived_at ? '&archive=1' : ''}`}
        className="th-v4-history-link"
        title={item.title}
        aria-current={selected ? 'page' : undefined}
        aria-hidden={expanded}
        inert={expanded}
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
        role="group"
        aria-label="Conversation actions"
        aria-hidden={!expanded}
        inert={!expanded}
      >
        {!item.archived_at && (
          <button
            type="button"
            title="Archive"
            aria-label="Archive"
            disabled={!connected}
            onClick={() => {
              close();
              onAction('archive');
            }}
          >
            <svg
              viewBox="0 0 16 16"
              width="16"
              height="16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M2 3h12v3H2z M3 6v7h10V6 M6.5 9h3" />
            </svg>
          </button>
        )}
        <button
          type="button"
          title="Delete"
          aria-label="Delete"
          className="th-history-delete"
          disabled={!connected}
          onClick={() => {
            close();
            onAction('delete');
          }}
        >
          <svg
            viewBox="0 0 16 16"
            width="16"
            height="16"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M2.5 4.5h11 M6 4.5V2.75h4V4.5 M4 4.5l.75 8.75h6.5L12 4.5 M7 7v3.5 M9 7v3.5" />
          </svg>
        </button>
      </div>
      <button
        ref={trigger}
        type="button"
        className="th-history-more"
        title={expanded ? 'Close actions' : 'Conversation actions'}
        aria-label={`${expanded ? 'Close actions for' : 'Actions for'} ${item.title}`}
        aria-expanded={expanded}
        aria-controls={panelId}
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
