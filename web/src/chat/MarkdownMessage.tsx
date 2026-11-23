import { isValidElement, useEffect, useRef, useState, type ReactNode } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

const copyLabels = {
  idle: 'Copy code',
  copied: 'Copied!',
  failed: 'Copy failed',
};
type CopyStatus = keyof typeof copyLabels;

function CodeBlock({ children }: { children?: ReactNode }) {
  const [status, setStatus] = useState<CopyStatus>('idle');
  const resetTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const pending = useRef<object | undefined>(undefined);
  useEffect(
    () => () => {
      pending.current = undefined;
      clearTimeout(resetTimer.current);
    },
    []
  );
  const code = isValidElement<{ children?: ReactNode; className?: string }>(children)
    ? children.props
    : undefined;
  const text = typeof code?.children === 'string' ? code.children.replace(/\n$/, '') : undefined;
  const language = code?.className?.match(/(?:^|\s)language-([^\s]+)/)?.[1].toLowerCase();
  const label =
    language === 'console' || language === 'terminal' || language === 'shell-session'
      ? 'Terminal'
      : language === 'output' || language === 'stdout' || language === 'stderr'
        ? 'Output'
        : 'Code';
  async function copy() {
    if (text === undefined || pending.current) return;
    const attempt = {};
    pending.current = attempt;
    clearTimeout(resetTimer.current);
    setStatus('idle');
    let result: CopyStatus = 'failed';
    try {
      await navigator.clipboard.writeText(text);
      result = 'copied';
    } catch {
      // Clipboard access may be unavailable or denied by the browser.
    }
    if (pending.current !== attempt) return;
    pending.current = undefined;
    setStatus(result);
    resetTimer.current = setTimeout(() => setStatus('idle'), 1800);
  }
  return (
    <div className="my-3 overflow-hidden rounded-xl border border-white/10 bg-[#111215]">
      <div className="relative flex items-center justify-between gap-2 border-b border-white/10 px-3 py-0.5 text-[11px] text-[#c7c8ce]">
        <span>{label}</span>
        {text !== undefined && (
          <button
            type="button"
            onClick={() => void copy()}
            className="th-code-copy"
            data-state={status}
          >
            <svg
              key={status}
              aria-hidden="true"
              width="13"
              height="13"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.35"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              {status === 'idle' && (
                <>
                  <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" />
                  <path d="M10 3H4.5A1.5 1.5 0 0 0 3 4.5V10" />
                </>
              )}
              {status === 'copied' && <path d="m3.5 8 3 3 6-6" />}
              {status === 'failed' && (
                <>
                  <circle cx="8" cy="8" r="5.5" />
                  <path d="M8 5.3v3M8 10.7h.01" />
                </>
              )}
            </svg>
            <span>{copyLabels[status]}</span>
          </button>
        )}
        <span role="status" className="sr-only">
          {status === 'copied' && 'Code copied to clipboard.'}
          {status === 'failed' && 'Copy failed. Try again or select and copy the text manually.'}
        </span>
      </div>
      <pre
        className="overflow-x-auto p-3 text-xs leading-6"
        tabIndex={0}
        aria-label={`${label} block`}
      >
        {children}
      </pre>
    </div>
  );
}

function renderCodeBlock({ children }: { children?: ReactNode }) {
  return <CodeBlock>{children}</CodeBlock>;
}

export function MarkdownMessage({ content }: { content: string }) {
  return (
    <div className="th-markdown min-w-0 break-words">
      <Markdown
        skipHtml
        remarkPlugins={[remarkGfm]}
        components={{
          pre: renderCodeBlock,
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
