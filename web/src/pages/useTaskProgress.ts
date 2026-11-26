import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { WebApiError, type WorkspaceApi } from '../api/client';
import { taskState, type SupervisorEvent, type SupervisorTask } from '../api/control';
import { approvalsKey, httpStatus, tasksKey } from './controlState';

const displayLimit = 200;
export const isTerminalTask = (state: SupervisorTask['state']) =>
  state === 'COMPLETED' || state === 'FAILED' || state === 'CANCELLED';

function subscribeVisibility(notify: () => void) {
  document.addEventListener('visibilitychange', notify);
  return () => document.removeEventListener('visibilitychange', notify);
}

function wait(ms: number, signal: AbortSignal) {
  return new Promise<void>(resolve => {
    const complete = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', complete);
      resolve();
    };
    const timer = setTimeout(complete, ms);
    signal.addEventListener('abort', complete, { once: true });
    if (signal.aborted) complete();
  });
}

// The parent keys the detail on task ID. Keep the cursor across reconnects, but never across tasks.
export function useTaskProgress(api: WorkspaceApi, taskId: string, connected: boolean) {
  const cache = useQueryClient();
  const visible = useSyncExternalStore(
    subscribeVisibility,
    () => document.visibilityState === 'visible'
  );
  const [task, setTask] = useState<SupervisorTask>();
  const [timeline, setTimeline] = useState<{ total: number; events: SupervisorEvent[] }>({
    total: 0,
    events: [],
  });
  const [mode, setMode] = useState<
    'loading' | 'following' | 'reconnecting' | 'history' | 'stopped'
  >('loading');
  const [error, setError] = useState<unknown>();
  const [refresh, setRefresh] = useState(0);
  const cursor = useRef(0);

  useEffect(() => {
    if (!connected || !visible) return;
    const controller = new AbortController();
    const { signal } = controller;
    let delay = 750;
    let fallback = false;
    let previousState: SupervisorTask['state'] | undefined;

    function append(rows: SupervisorEvent[]) {
      if (signal.aborted || !rows.length) return;
      cursor.current = rows[rows.length - 1].seq;
      setTimeline(previous => ({
        total: previous.total + rows.length,
        events: [...previous.events, ...rows].slice(-displayLimit),
      }));
    }

    async function replay() {
      while (!signal.aborted) {
        const page = await api.getTaskEvents(taskId, cursor.current, signal);
        append(page.events);
        if (page.events.length < displayLimit) return;
      }
    }

    function refreshLists() {
      void cache.invalidateQueries({ queryKey: tasksKey });
      void cache.invalidateQueries({ queryKey: approvalsKey });
    }

    function notifyState(state: SupervisorTask['state']) {
      if (state === previousState) return;
      if (
        state === 'WAITING_APPROVAL' ||
        previousState === 'WAITING_APPROVAL' ||
        isTerminalTask(state)
      )
        refreshLists();
      previousState = state;
    }

    async function follow() {
      while (!signal.aborted) {
        try {
          await replay();
          const snapshot = await api.getTask(taskId, signal);
          if (signal.aborted) return;
          setTask(snapshot);
          notifyState(snapshot.state);
          if (isTerminalTask(snapshot.state)) {
            await replay();
            if (signal.aborted) return;
            setError(undefined);
            setMode('history');
            return;
          }
        } catch (cause) {
          if (signal.aborted) return;
          setError(cause);
          if ([401, 403, 404].includes(httpStatus(cause) ?? 0)) {
            setTask(undefined);
            setTimeline({ total: 0, events: [] });
            cursor.current = 0;
            setMode('stopped');
            refreshLists();
            return;
          }
          setMode('reconnecting');
          await wait(delay, signal);
          delay = Math.min(delay * 2, 12_000);
          continue;
        }
        if (!fallback) {
          try {
            await api.streamTaskEvents(
              taskId,
              cursor.current,
              signal,
              () => {
                if (signal.aborted) return;
                setError(undefined);
                setMode('following');
              },
              item => {
                if (signal.aborted) return;
                append([item]);
                delay = 750;
                if (item.event_type === 'task.state') {
                  const state = taskState.safeParse(item.payload.to);
                  if (state.success) {
                    setTask(previous => (previous ? { ...previous, state: state.data } : previous));
                    notifyState(state.data);
                  }
                }
              }
            );
          } catch (cause) {
            if (signal.aborted) return;
            setError(cause);
            // A missing stream route need not make the durable event pages unavailable.
            fallback = [404, 405, 501].includes(httpStatus(cause) ?? 0);
            if (cause instanceof WebApiError && cause.kind === 'html') fallback = true;
          }
        }
        if (signal.aborted) return;
        setMode('reconnecting');
        await wait(delay, signal);
        delay = Math.min(delay * 2, 12_000);
      }
    }

    void follow();
    return () => controller.abort();
  }, [api, cache, connected, refresh, taskId, visible]);

  return { task, timeline, mode, error, retry: () => setRefresh(count => count + 1) };
}
