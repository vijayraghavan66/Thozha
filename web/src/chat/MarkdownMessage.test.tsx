import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
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
  expect(screen.getByRole('button', { name: 'Copy code' })).toBeVisible();
});
