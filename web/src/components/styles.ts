export const solid =
  'th-control th-press rounded-xl bg-[#e8e9ec] px-3 py-2 text-xs font-semibold text-[#17181b] hover:bg-white disabled:opacity-50';
export const outline =
  'th-control th-press rounded-xl border border-white/[.13] bg-white/[.055] px-3 py-2 text-xs text-[#eeeef0] hover:bg-white/[.11] disabled:opacity-50';
export const field =
  'th-field block w-full min-w-0 rounded-xl border border-white/15 bg-[#25262b] p-3 text-sm text-white placeholder:text-[#a1a2aa]';
export const muted = 'text-xs leading-6 text-[#a4a5af]';
export const formatDate = (value: string | number) =>
  new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(
    new Date(value)
  );
