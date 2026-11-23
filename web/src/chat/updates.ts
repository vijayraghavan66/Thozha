import { skipToken, useQuery } from '@tanstack/react-query';
import { mayHaveBeenAccepted, type ConversationDetail, type WorkspaceApi } from '../api/client';

export const detailKey = (id: string | null) => ['conversation', id] as const;
export const isPendingResponse = (status: string) => status === 'queued' || status === 'running';

export function snapshotInterval(
  snapshot: ConversationDetail | undefined,
  receipt: string | undefined,
  failures: number,
  error: unknown
) {
  if (error && !mayHaveBeenAccepted(error)) return false;
  const pending = snapshot?.messages.some(
    message => message.role === 'assistant' && isPendingResponse(message.status)
  );
  const acceptedNotLoaded = receipt && !snapshot?.messages.some(message => message.id === receipt);
  return pending || acceptedNotLoaded ? Math.min(12_000, 1_500 * 2 ** failures) : false;
}

export function useConversationSnapshot(
  read: WorkspaceApi['get'],
  id: string | null,
  enabled: boolean,
  receipt?: string,
  streaming = false
) {
  return useQuery({
    queryKey: detailKey(id),
    enabled,
    queryFn: id ? ({ signal }) => read(id, signal) : skipToken,
    refetchInterval: streaming
      ? false
      : query =>
          snapshotInterval(
            query.state.data,
            receipt,
            query.state.fetchFailureCount,
            query.state.error
          ),
    refetchIntervalInBackground: false,
    staleTime: 0,
  });
}
