export const slashCommands = [
  {
    name: 'task',
    usage: '/task <instruction>',
    description: 'Review a supervisor task before submitting.',
    availability: 'Requires a ready worker and an active mapped device.',
  },
  {
    name: 'status',
    usage: '/status',
    description: 'Check readiness reported by the host.',
    availability: 'Requires the supervisor readiness endpoint.',
  },
  {
    name: 'help',
    usage: '/help',
    description: 'Show commands and their availability.',
    availability: 'Available in the composer.',
  },
  {
    name: 'research',
    usage: '/research',
    description: 'Research current information.',
    availability: 'Not enabled.',
    deferred: true,
  },
  {
    name: 'code',
    usage: '/code',
    description: 'Start a code workflow.',
    availability: 'Not enabled.',
    deferred: true,
  },
  {
    name: 'run',
    usage: '/run',
    description: 'Run an authorized workflow.',
    availability: 'Not enabled.',
    deferred: true,
  },
  {
    name: 'tasks',
    usage: '/tasks',
    description: 'Browse supervisor tasks.',
    availability: 'Not enabled; use Tasks in the sidebar.',
    deferred: true,
  },
  {
    name: 'memory',
    usage: '/memory',
    description: 'Open memory tools.',
    availability: 'Not enabled; use Memory Center in the sidebar.',
    deferred: true,
  },
] as const;

export function parseSlash(draft: string) {
  if (!draft.startsWith('/')) return undefined;
  const separator = draft.search(/\s/);
  const name = draft.slice(1, separator < 0 ? undefined : separator).toLowerCase();
  return {
    name,
    argument: separator < 0 ? '' : draft.slice(separator).trim(),
    completing: separator < 0,
    command: slashCommands.find(entry => entry.name === name),
  };
}

export function titleFromMessage(text: string) {
  const summary = text.replace(/\s+/g, ' ').trim();
  const chars = Array.from(summary);
  if (chars.length <= 64) return summary;
  const prefix = chars.slice(0, 61).join('');
  const boundary = prefix.lastIndexOf(' ');
  return `${boundary > 32 ? prefix.slice(0, boundary) : prefix}…`;
}
