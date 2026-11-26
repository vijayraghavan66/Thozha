import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MarkdownMessage } from './MarkdownMessage';

it('renders Markdown but never loads inline images or enables raw HTML', () => {
  render(
    <MarkdownMessage
      content={
        '**Strong text**\n\n![A diagram](https://example.invalid/diagram.png)\n\n<button>Ignore me</button>\n\n```ts\nconst demo = 1;\n```'
      }
    />
  );
  expect(screen.getByText('Strong text').tagName).toBe('STRONG');
  expect(screen.getByText('[Image: A diagram]')).toBeVisible();
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Ignore me' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Copy code/ })).toBeVisible();
});

it('makes wide response tables keyboard-scrollable without expanding the chat', () => {
  render(<MarkdownMessage content={'| Name | Value |\n| --- | --- |\n| Example | Long value |'} />);
  const region = screen.getByRole('region', { name: 'Scrollable table' });
  expect(region).toHaveAttribute('tabindex', '0');
  expect(region).toContainElement(screen.getByRole('table'));
  region.focus();
  expect(region).toHaveFocus();
});

it('labels fenced terminal and output blocks without reclassifying source languages', () => {
  render(
    <MarkdownMessage
      content={
        '```bash\necho "not a terminal transcript"\n```\n\n```console\nuser@host$ whoami\n```\n\n```stderr\ndenied\n```'
      }
    />
  );
  expect(screen.getByLabelText('Code block')).toHaveTextContent('echo');
  expect(screen.getByLabelText('Terminal block')).toHaveTextContent('user@host');
  expect(screen.getByLabelText('Output block')).toHaveTextContent('denied');
  expect(screen.getAllByRole('button', { name: /Copy code/ })).toHaveLength(3);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it('waits for the clipboard, ignores overlapping clicks, and resets only the active block', async () => {
  vi.useFakeTimers();
  let resolveWrite!: () => void;
  const writeText = vi.fn(
    () =>
      new Promise<void>(resolve => {
        resolveWrite = resolve;
      })
  );
  vi.stubGlobal('navigator', { clipboard: { writeText } });
  render(<MarkdownMessage content={'```\nfirst\n```\n\n    second\n'} />);
  const [first, second] = screen.getAllByRole('button', { name: /Copy code/ });
  fireEvent.click(first);
  fireEvent.click(first);
  expect(writeText).toHaveBeenCalledExactlyOnceWith('first');
  expect(first).toHaveAccessibleName('Copy code');
  await act(async () => resolveWrite());
  expect(first).toHaveAccessibleName('Copied!');
  expect(second).toHaveAccessibleName('Copy code');
  expect(screen.getAllByRole('status')[0]).toHaveTextContent('Code copied to clipboard.');

  act(() => vi.advanceTimersByTime(1700));
  fireEvent.click(first);
  await act(async () => resolveWrite());
  act(() => vi.advanceTimersByTime(100));
  expect(first).toHaveAccessibleName('Copied!');
  act(() => vi.advanceTimersByTime(1700));
  expect(first).toHaveAccessibleName('Copy code');
});

it('announces a denied write, allows retry, and clears feedback on unmount', async () => {
  vi.useFakeTimers();
  const writeText = vi
    .fn<() => Promise<void>>()
    .mockRejectedValueOnce(new DOMException('Permission denied', 'NotAllowedError'))
    .mockResolvedValueOnce(undefined);
  vi.stubGlobal('navigator', { clipboard: { writeText } });
  const { unmount } = render(<MarkdownMessage content={'```\nretry me\n```'} />);
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Copy code' })));
  expect(screen.getByRole('status')).toHaveTextContent('Copy failed. Try again');
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Copy failed' })));
  expect(screen.getByRole('button', { name: 'Copied!' })).toBeVisible();
  expect(vi.getTimerCount()).toBe(1);
  unmount();
  expect(vi.getTimerCount()).toBe(0);
});
