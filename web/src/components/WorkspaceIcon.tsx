const paths = {
  'AI Workspace': 'M5 5h14v11H9l-4 3V5Z M9 9h6 M9 12h4',
  Tasks: 'M8 5h11v15H5V5h3 M8 3h8v4H8V3Z M9 11h6 M9 15h4',
  Agents:
    'M5 5h14v14H5V5Z M9 9h6v6H9V9Z M9 2v3 M15 2v3 M9 19v3 M15 19v3 M2 9h3 M19 9h3 M2 15h3 M19 15h3',
  Approvals: 'M12 3 4 6v6c0 4 8 9 8 9s8-5 8-9V6l-8-3Z M8 12l3 3 5-6',
  Automations: 'M4 5h6v5H4V5Z M14 14h6v5h-6v-5Z M10 7h7v7 M7 10v7h7',
  'Memory Center': 'M5 4h14v16H5V4Z M9 4v16 M12 9h4 M12 13h4',
  'Device Hub': 'M3 4h13v11H3V4Z M7 19h5 M10 15v4 M18 9h4v11h-7v-3',
  'Developer Studio': 'm8 7-5 5 5 5 m8-10 5 5-5 5 M14 4l-4 16',
  'Trading Lab': 'M4 4v16h16 M7 15l4-5 4 3 5-7',
  Settings: 'M4 7h16 M4 17h16 M8 4v6 M16 14v6',
  Explore: 'M4 4h6v6H4V4Z M14 4h6v6h-6V4Z M4 14h6v6H4v-6Z M14 14h6v6h-6v-6Z',
  attach: 'm8 13 7-7a3 3 0 0 1 4 4l-9 9a5 5 0 0 1-7-7l9-9 M6 14l8-8',
  send: 'M12 19V5 M6 11l6-6 6 6',
  stop: 'M6 6h12v12H6V6Z',
  sidebar: 'M3 4h18v16H3V4Z M9 4v16',
  history: 'M4 10a8 8 0 1 1 1 7 M4 4v6h6 M12 8v5l3 2',
  context: 'M7 3h7l4 4v14H5V3h2Z M14 3v5h4 M8 12h7 M8 16h5',
  new: 'M12 5v14 M5 12h14',
} as const;

export function WorkspaceIcon({ name }: { name: keyof typeof paths }) {
  return (
    <svg
      aria-hidden="true"
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={paths[name]} />
    </svg>
  );
}
