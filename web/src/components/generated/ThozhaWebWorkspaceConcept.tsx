import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import type { WorkspaceApi } from '../../api/client';
import {
  Blueprint,
  DeveloperStudio,
  DeviceHub,
  Overview,
  Settings,
} from '../../pages/WorkspacePages';
import { Chip, Crest, Loading, Modal, Notice } from '../ui';
import { field, muted } from '../styles';

const ConversationWorkspace = lazy(() => import('../../chat/ConversationWorkspace'));
const TradingLab = lazy(() => import('../../trading/TradingLab'));
const MemoryCenter = lazy(() => import('../../memory/MemoryCenter'));
const sections = [
  ['Overview', 'overview', '◫'],
  ['AI Workspace', 'ai-workspace', '◉'],
  ['Trading Lab', 'trading-lab', '⌁'],
  ['Device Hub', 'device-hub', '▣'],
  ['Automations', 'automations', '◇'],
  ['Memory Center', 'memory-center', '◎'],
  ['Developer Studio', 'developer-studio', '⌘'],
  ['Settings', 'settings', '⚙'],
] as const;
type Section = (typeof sections)[number][0];
const currentPage = () =>
  sections.find(([, slug]) => window.location.hash === `#/${slug}`)?.[0] ?? 'Overview';
const motionKey = 'thozha-web.motion.v1';

function motionPreference() {
  try {
    return { reduced: localStorage.getItem(motionKey) === 'reduce', problem: '' };
  } catch {
    return {
      reduced: false,
      problem: 'Browser preferences are unavailable. Changes apply until this page is reloaded.',
    };
  }
}

export function ThozhaWebWorkspaceConcept({
  api,
  userId,
  expiresAt,
  problem,
  checkedAt,
  logout,
  checkSession,
}: {
  api: WorkspaceApi;
  userId: string;
  expiresAt: number;
  problem?: string;
  checkedAt: number;
  logout: () => void;
  checkSession: () => void;
}) {
  const [page, setPage] = useState<Section>(currentPage);
  const [visitedChat, setVisitedChat] = useState(() => currentPage() === 'AI Workspace');
  const [dialog, setDialog] = useState<'navigation' | 'search' | 'notifications' | null>(null);
  const [query, setQuery] = useState('');
  const [motion, setMotion] = useState(motionPreference);
  const main = useRef<HTMLElement>(null);
  const searchList = useRef<HTMLDivElement>(null);
  const focusAfterNavigation = useRef(false);
  const matching = sections.filter(([name]) =>
    name.toLowerCase().includes(query.trim().toLowerCase())
  );

  useEffect(() => {
    function onNavigate() {
      const next = currentPage();
      focusAfterNavigation.current = true;
      setPage(next);
      if (next === 'AI Workspace') setVisitedChat(true);
      setDialog(null);
    }
    window.addEventListener('hashchange', onNavigate);
    return () => window.removeEventListener('hashchange', onNavigate);
  }, []);
  useEffect(() => {
    document.title = `Thozha · ${page}`;
    if (!focusAfterNavigation.current) return;
    focusAfterNavigation.current = false;
    main.current?.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: 'instant' });
  }, [page]);
  useEffect(() => {
    function shortcut(event: KeyboardEvent) {
      if (
        !(event.metaKey || event.ctrlKey) ||
        event.key.toLowerCase() !== 'k' ||
        document.querySelector('dialog[open]')
      )
        return;
      event.preventDefault();
      setQuery('');
      setDialog('search');
    }
    window.addEventListener('keydown', shortcut);
    return () => window.removeEventListener('keydown', shortcut);
  }, []);

  function go(next: Section) {
    const section = sections.find(([name]) => name === next);
    if (!section) return;
    setDialog(null);
    const hash = `#/${section[1]}`;
    if (window.location.hash !== hash) window.location.hash = hash;
  }

  function toggleMotion() {
    const reduced = !motion.reduced;
    try {
      localStorage.setItem(motionKey, reduced ? 'reduce' : 'system');
      setMotion({ reduced, problem: '' });
    } catch {
      setMotion({
        reduced,
        problem: 'Browser preferences are unavailable. Changes apply until this page is reloaded.',
      });
    }
  }

  function navigation() {
    return (
      <nav aria-label="Main navigation" className="space-y-1">
        {sections.map(([name, slug, symbol], index) => (
          <div key={slug}>
            {index === 5 && (
              <div className="px-3 pb-2 pt-7 text-[10px] tracking-[.18em] text-[#a4a5af]">
                SYSTEM
              </div>
            )}
            <a
              href={`#/${slug}`}
              onClick={() => setDialog(null)}
              aria-current={page === name ? 'page' : undefined}
              className={`flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left text-[12px] transition ${
                page === name
                  ? 'border border-white/[.1] bg-gradient-to-r from-white/[.15] to-white/[.055] font-semibold text-white shadow-sm'
                  : 'border border-transparent text-[#a0a2a9] hover:bg-white/[.06] hover:text-white'
              }`}
            >
              <span aria-hidden="true" className="w-5 text-center text-lg font-light">
                {symbol}
              </span>
              <span>{name}</span>
            </a>
          </div>
        ))}
      </nav>
    );
  }

  return (
    <div
      className={`th-workspace min-h-dvh bg-[#08090b] p-3 text-[#ececf0] selection:bg-white/25 ${motion.reduced ? 'th-stop-motion' : ''}`}
    >
      <a
        href="#main-content"
        onClick={event => {
          event.preventDefault();
          main.current?.focus();
        }}
        className="sr-only z-50 focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:rounded-lg focus:bg-[#25262b] focus:p-4"
      >
        Skip to content
      </a>
      <div className="relative mx-auto flex min-h-[calc(100dvh-1.5rem)] max-w-[1550px] overflow-hidden rounded-[24px] border border-white/[.1] bg-[#101114] shadow-[0_40px_130px_rgba(0,0,0,.5)] lg:min-h-[825px]">
        <aside className="z-30 hidden w-[237px] shrink-0 flex-col border-r border-white/[.08] bg-[#131417] p-4 lg:flex">
          <div className="mb-10 flex items-center gap-3 px-2 pt-3">
            <Crest />
            <div>
              <div className="text-[21px] font-semibold tracking-[-.06em]">
                thozha<span className="text-[#858690]">.</span>
              </div>
              <p className="text-[9px] font-semibold tracking-[.17em] text-[#a4a5af]">
                PERSONAL WORKSPACE
              </p>
            </div>
          </div>
          <div className="mb-3 px-3 text-[10px] font-semibold tracking-[.18em] text-[#a4a5af]">
            WORKSPACE
          </div>
          {navigation()}
          <div className="mt-auto rounded-2xl border border-white/[.08] bg-[#1c1d21] p-3">
            <div className="flex items-center gap-3">
              <span
                aria-hidden="true"
                className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-[#3a3b42] text-xs font-semibold"
              >
                {userId.slice(0, 1).toUpperCase()}
              </span>
              <div className="min-w-0">
                <p className="truncate text-xs font-medium">Personal workspace</p>
                <p className="mt-1 text-[10px] text-[#a4a5af]">
                  {problem ? 'Connection uncertain' : 'Signed in · Web v1'}
                </p>
              </div>
            </div>
          </div>
        </aside>
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex min-h-[70px] shrink-0 items-center justify-between gap-1 border-b border-white/[.08] px-8 max-md:px-3">
            <div className="flex min-w-0 items-center gap-2 sm:gap-3">
              <button
                type="button"
                className="min-h-10 min-w-9 rounded-lg hover:bg-white/10 lg:hidden"
                aria-label="Open navigation"
                aria-expanded={dialog === 'navigation'}
                onClick={() => setDialog('navigation')}
              >
                ☰
              </button>
              <span className="hidden text-xs text-[#a4a5af] sm:inline">Workspace</span>
              <span aria-hidden="true" className="hidden text-[#777985] sm:inline">
                /
              </span>
              <span className="truncate text-xs font-medium">{page}</span>
            </div>
            <div className="flex shrink-0 items-center gap-1 sm:gap-3">
              <Chip>WEB ALPHA</Chip>
              <button
                type="button"
                className="min-h-10 min-w-9 rounded-lg text-xl text-[#b4b4bb] hover:bg-white/10"
                aria-label="Search pages"
                aria-keyshortcuts="Control+k Meta+k"
                onClick={() => {
                  setQuery('');
                  setDialog('search');
                }}
              >
                ⌕
              </button>
              <button
                type="button"
                className="min-h-10 min-w-9 rounded-lg text-base text-[#b4b4bb] hover:bg-white/10"
                aria-label="Notifications"
                onClick={() => setDialog('notifications')}
              >
                ♧
              </button>
            </div>
          </header>
          <main
            id="main-content"
            tabIndex={-1}
            ref={main}
            className="relative flex-1 px-9 py-9 outline-none max-md:px-4 max-md:py-6"
          >
            {problem && (
              <div className="mb-5">
                <Notice error retry={checkSession}>
                  {problem}
                </Notice>
              </div>
            )}
            {page !== 'AI Workspace' && (
              <div key={page} className="th-enter">
                {page === 'Overview' && (
                  <Overview
                    onChat={() => go('AI Workspace')}
                    onDevices={() => go('Device Hub')}
                    onSettings={() => go('Settings')}
                    connected={!problem}
                  />
                )}
                {page === 'Trading Lab' && (
                  <Suspense fallback={<Loading label="Loading Trading Lab…" />}>
                    <TradingLab />
                  </Suspense>
                )}
                {page === 'Device Hub' && <DeviceHub />}
                {page === 'Automations' && <Blueprint />}
                {page === 'Memory Center' && (
                  <Suspense fallback={<Loading label="Loading Memory Center…" />}>
                    <MemoryCenter api={api} connected={!problem} />
                  </Suspense>
                )}
                {page === 'Developer Studio' && <DeveloperStudio />}
                {page === 'Settings' && (
                  <Settings
                    userId={userId}
                    expiresAt={expiresAt}
                    checkedAt={checkedAt}
                    connected={!problem}
                    reduced={motion.reduced}
                    motionProblem={motion.problem}
                    toggleMotion={toggleMotion}
                    checkSession={checkSession}
                    logout={logout}
                  />
                )}
              </div>
            )}
            {(visitedChat || page === 'AI Workspace') && (
              <div hidden={page !== 'AI Workspace'} className="th-enter">
                <Suspense fallback={<Loading label="Loading AI Workspace…" />}>
                  <ConversationWorkspace
                    api={api}
                    active={page === 'AI Workspace'}
                    connected={!problem}
                    reduced={motion.reduced}
                  />
                </Suspense>
              </div>
            )}
          </main>
        </div>
      </div>
      {dialog === 'navigation' && (
        <Modal
          title="Workspace navigation"
          close={() => setDialog(null)}
          cls="mr-auto ml-3 min-h-[70dvh] max-w-[275px] bg-[#131417]"
        >
          {navigation()}
        </Modal>
      )}
      {dialog === 'search' && (
        <Modal title="Search pages" close={() => setDialog(null)} cls="mt-[14dvh]">
          <input
            data-dialog-focus
            className={field}
            aria-label="Jump to page"
            placeholder="Jump to…"
            value={query}
            onChange={event => setQuery(event.target.value)}
            onKeyDown={event => {
              if (event.nativeEvent.isComposing) return;
              if (event.key === 'Enter' && matching[0]) go(matching[0][0]);
              if (event.key === 'ArrowDown') {
                event.preventDefault();
                searchList.current?.querySelector('button')?.focus();
              }
            }}
          />
          <div ref={searchList} className="mt-2">
            {matching.map(([name]) => (
              <button
                type="button"
                className="block w-full rounded-xl px-3 py-3 text-left text-xs hover:bg-white/10"
                onClick={() => go(name)}
                key={name}
              >
                {name} <span aria-hidden="true">↗</span>
              </button>
            ))}
            {!matching.length && (
              <p role="status" className={`p-3 ${muted}`}>
                No matching pages.
              </p>
            )}
          </div>
        </Modal>
      )}
      {dialog === 'notifications' && (
        <Modal title="Notifications" close={() => setDialog(null)}>
          <Chip>UNAVAILABLE IN WEB v1</Chip>
          <p className={`mt-3 ${muted}`}>
            The browser API does not expose notification history or unread counts.
          </p>
        </Modal>
      )}
    </div>
  );
}
