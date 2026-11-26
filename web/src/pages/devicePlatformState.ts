import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { WorkspaceApi } from '../api/client';
import { inventoryUnavailable } from '../api/devicePlatform';

export function useDevicePlatform(api: WorkspaceApi, connected: boolean) {
  const [browserOnline, setBrowserOnline] = useState(() => navigator.onLine);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const update = () => setBrowserOnline(navigator.onLine);
    window.addEventListener('online', update);
    window.addEventListener('offline', update);
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => {
      window.removeEventListener('online', update);
      window.removeEventListener('offline', update);
      window.clearInterval(timer);
    };
  }, []);
  const online = connected && browserOnline;
  const inventory = useQuery({
    queryKey: ['web-control', 'device-platform', 'v1'],
    queryFn: ({ signal }) => api.devicePlatformInventory(signal),
    enabled: online,
    retry: (count, error) => !inventoryUnavailable(error) && count < 1,
    refetchInterval: query => (online && !inventoryUnavailable(query.state.error) ? 15_000 : false),
    refetchIntervalInBackground: false,
    refetchOnReconnect: 'always',
  });
  return { inventory, online, now };
}
