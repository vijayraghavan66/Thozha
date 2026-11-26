import { useQuery } from '@tanstack/react-query';
import { errorText, WebApiError, type WorkspaceApi } from '../api/client';

export const devicesKey = ['web-control', 'devices'] as const;
export const tasksKey = ['web-control', 'tasks'] as const;
export const approvalsKey = ['web-control', 'approvals'] as const;
export const automationsKey = ['web-control', 'automations'] as const;
export const automationCapabilitiesKey = ['web-control', 'automation-capabilities'] as const;

export function useDevices(api: WorkspaceApi, enabled: boolean) {
  return useQuery({
    queryKey: devicesKey,
    queryFn: ({ signal }) => api.listDevices(signal),
    enabled,
  });
}

export function useTasks(
  api: WorkspaceApi,
  state: 'active' | 'completed' | 'all',
  enabled: boolean
) {
  return useQuery({
    queryKey: [...tasksKey, state],
    queryFn: ({ signal }) => api.listTasks(state, state === 'active' ? 100 : 50, signal),
    enabled,
  });
}

export function useApprovals(api: WorkspaceApi, enabled: boolean) {
  return useQuery({
    queryKey: approvalsKey,
    queryFn: ({ signal }) => api.listApprovals(signal),
    enabled,
  });
}

export function httpStatus(error: unknown) {
  return error instanceof WebApiError ? error.status : undefined;
}

export function controlError(error: unknown, action: string) {
  if (error instanceof WebApiError && error.detail) return error.detail;
  if (httpStatus(error) === 503)
    return `The backend could not ${action}. The service may be disabled or unavailable.`;
  return errorText(error, action);
}

export function taskHref(taskId: string) {
  return `#/tasks?task=${encodeURIComponent(taskId)}`;
}
