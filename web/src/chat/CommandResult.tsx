import { useQuery } from '@tanstack/react-query';
import type { WorkspaceApi } from '../api/client';
import { Modal, Notice } from '../components/ui';
import { outline } from '../components/styles';
import {
  readinessError,
  readinessReason,
  taskReadinessBlocker,
} from '../pages/supervisorReadiness';
import { slashCommands } from './slashCommands';

export function CommandResult({
  mode,
  api,
  close,
}: {
  mode: 'status' | 'help';
  api: WorkspaceApi;
  close: () => void;
}) {
  const status = useQuery({
    queryKey: ['composer-readiness'],
    queryFn: ({ signal }) => api.supervisorReadiness(signal),
    enabled: mode === 'status',
    staleTime: 0,
    retry: false,
  });
  const report = status.isSuccess && !status.isFetching ? status.data : undefined;
  const components = report?.components;
  const rows = components
    ? ([
        [
          'Device mapping',
          components.device_mapping,
          `${components.device_mapping.active_device_count} active`,
        ],
        ['Model engine', components.engine, components.engine.check],
        ['Supervisor worker', components.supervisor_worker],
        ['Automation scheduler', components.automation_scheduler],
      ] as const)
    : [];
  return (
    <Modal title={mode === 'help' ? 'Composer commands' : 'Host readiness'} close={close}>
      {mode === 'help' ? (
        <ul className="space-y-3 text-xs">
          {slashCommands.map(command => (
            <li key={command.name}>
              <code>{command.usage}</code>
              <p className="mt-1 text-[#bfc1cb]">
                {command.description} {command.availability}
              </p>
            </li>
          ))}
        </ul>
      ) : (
        <div className="space-y-3 text-xs">
          {status.isFetching && <p role="status">Checking the host…</p>}
          {status.isError && !status.isFetching && (
            <Notice error>{readinessError(status.error)}</Notice>
          )}
          {report && components && (
            <>
              <p>
                Host reports: <strong>{report.status}</strong>
              </p>
              <dl className="space-y-2">
                <div>
                  <dt>Browser session</dt>
                  <dd>{components.authentication.browser_session}</dd>
                </div>
                <div>
                  <dt>Codex SDK authentication</dt>
                  <dd>{components.authentication.codex_sdk.replace(/_/g, ' ')}</dd>
                </div>
                {rows.map(([label, component, detail]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>
                      {component.state}
                      {detail ? ` · ${detail}` : ''}
                      {component.reason ? ` — ${readinessReason(component.reason)}` : ''}
                    </dd>
                  </div>
                ))}
              </dl>
              {taskReadinessBlocker(report) && <Notice>{taskReadinessBlocker(report)}</Notice>}
            </>
          )}
          <button
            type="button"
            className={outline}
            disabled={status.isFetching}
            onClick={() => void status.refetch()}
          >
            Refresh readiness
          </button>
        </div>
      )}
    </Modal>
  );
}
