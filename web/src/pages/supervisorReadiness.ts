import { type SupervisorReadiness } from '../api/control';
import { httpStatus } from './controlState';

const reasonLabels: Record<string, string> = {
  device_mapping_unavailable: 'No active device is explicitly mapped to this Web owner.',
  paired_device_store_unavailable: 'The paired-device store is unavailable.',
  runner_unavailable: 'The supervisor worker is not running.',
  engine_not_configured: 'The host has no model configured.',
  codex_auth_not_verified_this_process:
    'Codex authentication has not been verified by a model turn in this process.',
  codex_auth_missing: 'Codex SDK credentials are missing on the host.',
  automation_disabled: 'Scheduling is disabled on the host.',
};

export function readinessReason(reason: string) {
  return Object.prototype.hasOwnProperty.call(reasonLabels, reason) ? reasonLabels[reason] : reason;
}

export function taskReadinessBlocker({ components }: SupervisorReadiness) {
  const { device_mapping: mapping, supervisor_worker: worker } = components;
  if (mapping.state !== 'ready' || !mapping.active_device_count)
    return `No active device mapping was verified. Check Device Hub and ask the host administrator to map an active pairing to this Web owner.${mapping.reason ? ` ${readinessReason(mapping.reason)}` : ''}`;
  if (worker.state !== 'ready')
    return `Supervisor worker is not ready. Ask the host administrator to check the worker, then retry.${worker.reason ? ` Host: ${readinessReason(worker.reason)}` : ''}`;
  if (components.engine.state === 'unavailable')
    return `The host model is unavailable. Ask the host administrator to check the model configuration.${components.engine.reason ? ` Host: ${readinessReason(components.engine.reason)}` : ''}`;
  return undefined;
}

export function readinessError(error: unknown) {
  if (httpStatus(error) === 404)
    return 'This host does not expose supervisor readiness. Update the host to a compatible version.';
  if (httpStatus(error) === 401 || httpStatus(error) === 403)
    return 'The host did not authorize this readiness check. Check the Web owner session.';
  return 'Could not verify supervisor readiness. Check the host connection and retry.';
}
