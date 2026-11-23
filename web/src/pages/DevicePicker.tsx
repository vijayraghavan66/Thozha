import type { PairedDevice } from '../api/control';
import { field } from '../components/styles';

export function DevicePicker({
  devices,
  value,
  change,
  disabled,
  label = 'Requester device',
}: {
  devices: PairedDevice[];
  value: string;
  change: (id: string) => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <label className="block text-xs">
      {label}
      <select
        value={value}
        required
        disabled={disabled}
        onChange={event => change(event.target.value)}
        className={field + ' mt-2'}
      >
        <option value="">Choose a mapped device</option>
        {devices.map(device => (
          <option key={device.device_id} value={device.device_id}>
            {device.device_name} ({device.device_id.slice(0, 8)})
          </option>
        ))}
      </select>
    </label>
  );
}
