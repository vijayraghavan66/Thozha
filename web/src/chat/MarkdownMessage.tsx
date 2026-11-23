import { isValidElement, useState, type ReactNode } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

function CodeBlock({ children }: { children?: ReactNode }) {
  const [status, setStatus] = useState('');
  const text =
    isValidElement<{ children?: ReactNode }>(children) &&
    typeof children.props.children === 'string'
      ? children.props.children.replace(/\n$/, '')
      : undefined;
  async function copy() {
    if (text === undefined) return;
    try {
      await navigator.clipboard.writeText(text);
      setStatus('Copied');
    } catch {
      setStatus('Could not copy. Select and copy the code manually.');
    }
  }
  return (
    <div className="my-3 overflow-hidden rounded-xl border border-white/10 bg-[#111215]">
      <div className="flex min-h-8 flex-wrap items-center justify-between gap-2 border-b border-white/10 px-3 text-[11px] text-[#c7c8ce]">
        <span>Code</span>
        {text !== undefined && (
          <button
            type="button"
            onClick={() => void copy()}
            className="rounded px-2 py-1 hover:bg-white/10"
          >
            Copy code
          </button>
        )}
      </div>
      <pre className="overflow-x-auto p-3 text-xs leading-6" tabIndex={0} aria-label="Code block">
        {children}
      </pre>
      <p role="status" className={status ? 'px-3 pb-2 text-[11px] text-[#c7c8ce]' : 'sr-only'}>
        {status}
      </p>
    </div>
  );
}

export function MarkdownMessage({ content }: { content: string }) {
  return (
    <div className="th-markdown min-w-0 break-words">
      <Markdown
        skipHtml
        remarkPlugins={[remarkGfm]}
        components={{
          pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
          img: ({ alt }) => <span>{alt ? `[Image: ${alt}]` : '[Image omitted]'}</span>,
          a: ({ children, href }) =>
            href ? (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
          table: ({ children }) => (
            <div className="overflow-x-auto">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {content}
      </Markdown>
    </div>
  );
}
