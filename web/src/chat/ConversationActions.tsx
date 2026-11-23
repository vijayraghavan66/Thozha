import { useId, useRef, useState } from 'react';
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
        {rows.map(item => {
          const expanded = open === item.id;
          const panelId = `${actionsId}-${item.id}`;
          return (
            <li
              key={item.id}
              onBlur={event => {
                if (expanded && !event.currentTarget.contains(event.relatedTarget))
                  setOpen(undefined);
              }}
              onKeyDown={event => {
                if (!expanded || event.key !== 'Escape') return;
                event.preventDefault();
                event.stopPropagation();
                event.currentTarget.querySelector('button')?.focus();
                setOpen(undefined);
              }}
            >
              <div className="th-history-row">
                <a
                  href={`#/ai-workspace?conversation=${encodeURIComponent(item.id)}${archived ? '&archive=1' : ''}`}
                  className="th-v4-history-link"
                  aria-current={selectedId === item.id ? 'page' : undefined}
                  onClick={() => {
                    setOpen(undefined);
                    onSelect?.();
                  }}
                >
                  {item.title}
                </a>
                <button
                  type="button"
                  className="th-history-more"
                  aria-label={`Actions for ${item.title}`}
                  aria-expanded={expanded}
                  aria-controls={panelId}
                  onClick={event => {
                    event.currentTarget.focus({ preventScroll: true });
                    setOpen(expanded ? undefined : item.id);
                  }}
                >
                  <span aria-hidden="true">···</span>
                </button>
              </div>
              <div id={panelId} hidden={!expanded} className="th-history-actions">
                {(!archived ? (['archive', 'delete'] as const) : (['delete'] as const)).map(
                  type => (
                    <button
                      key={type}
                      type="button"
                      disabled={!connected}
                      onClick={event => {
                        const row = event.currentTarget.closest('li');
                        row?.querySelector('button')?.focus();
                        setOpen(undefined);
                        onAction({ type, conversation: item }, historyToggle.current);
                      }}
                    >
                      {type === 'archive' ? 'Archive' : 'Delete'}
                    </button>
                  )
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
