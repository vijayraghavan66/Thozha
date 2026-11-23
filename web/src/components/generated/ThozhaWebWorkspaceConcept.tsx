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
import { AnchoredSurface, NotificationPanel, TravelingSurface } from '../LiquidSurface';
import { muted } from '../styles';

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

function SearchGlyph({ open }: { open: boolean }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="h-[18px] w-[18px] overflow-visible">
      <circle
        className={`th-search-glass${open ? ' is-open' : ''}`}
        cx="10.5"
        cy="10.5"
        r="6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.7"
      />
      <path
        className={`th-search-handle${open ? ' is-open' : ''}`}
        d="m7 7 10 10"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.7"
      />
      <path
        className={`th-search-cross th-search-cross-b${open ? ' is-open' : ''}`}
        d="m17 7-10 10"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.7"
      />
    </svg>
  );
}

function MenuGlyph({ open }: { open: boolean }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="h-[18px] w-[18px]">
      <path className={`th-menu-line th-menu-top${open ? ' is-open' : ''}`} d="M5 8h14" />
      <path className={`th-menu-line th-menu-middle${open ? ' is-open' : ''}`} d="M5 12h14" />
      <path className={`th-menu-line th-menu-bottom${open ? ' is-open' : ''}`} d="M5 16h14" />
    </svg>
  );
}

function WorkspaceNavigation({
  page,
  drawer = false,
  onNavigate,
}: {
  page: Section;
  drawer?: boolean;
  onNavigate: () => void;
}) {
  const nav = useRef<HTMLElement>(null);
  return (
    <nav ref={nav} aria-label="Main navigation" className="relative space-y-1">
      <TravelingSurface container={nav} selection={page} />
      {sections.map(([name, slug, symbol], index) => (
        <div key={slug} className="relative z-[1]">
          {index === 5 && (
            <div className="px-3 pb-2 pt-7 text-[10px] tracking-[.18em] text-[#a4a5af]">SYSTEM</div>
          )}
          <a
            href={`#/${slug}`}
            data-dialog-focus={drawer && index === 0 ? '' : undefined}
            onClick={onNavigate}
            aria-current={page === name ? 'page' : undefined}
            className={`flex w-full items-center gap-3 rounded-xl border border-transparent px-3 py-3 text-left text-[12px] transition-colors duration-200 ease-out ${
              page === name
                ? 'font-semibold text-white'
                : 'text-[#a0a2a9] hover:bg-white/[.06] hover:text-white'
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
  const [dialog, setDialog] = useState<'navigation' | null>(null);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [drawerClosing, setDrawerClosing] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeResult, setActiveResult] = useState(0);
  const [motion, setMotion] = useState(motionPreference);
  const main = useRef<HTMLElement>(null);
  const searchAnchor = useRef<HTMLDivElement>(null);
  const searchTrigger = useRef<HTMLButtonElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const focusAfterNavigation = useRef(false);
  const drawerClosingRef = useRef(false);
  const drawerCloseTimer = useRef<number | null>(null);
  const matching = sections.filter(([name]) =>
    name.toLowerCase().includes(query.trim().toLowerCase())
  );

  useEffect(() => {
    function onNavigate() {
      const next = currentPage();
      focusAfterNavigation.current = true;
      setPage(next);
      if (next === 'AI Workspace') setVisitedChat(true);
      if (!drawerClosingRef.current) setDialog(null);
      setSearchOpen(false);
      setNotificationsOpen(false);
    }
    window.addEventListener('hashchange', onNavigate);
    return () => {
      window.removeEventListener('hashchange', onNavigate);
      if (drawerCloseTimer.current !== null) window.clearTimeout(drawerCloseTimer.current);
    };
  }, []);
  useEffect(() => {
    document.title = `Thozha · ${page}`;
    if (!focusAfterNavigation.current) return;
    focusAfterNavigation.current = false;
    main.current?.focus({ preventScroll: true });
    main.current?.scrollTo({ top: 0, behavior: 'instant' });
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
      setActiveResult(0);
      setDialog(null);
      setNotificationsOpen(false);
      setSearchOpen(true);
    }
    window.addEventListener('keydown', shortcut);
    return () => window.removeEventListener('keydown', shortcut);
  }, []);

  useEffect(() => {
    if (!searchOpen) return;
    searchInput.current?.focus();
    function onPointerDown(event: PointerEvent) {
      if (!searchAnchor.current?.contains(event.target as Node)) setSearchOpen(false);
    }
    function onEscape(event: KeyboardEvent) {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      closeSearch(true);
    }
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onEscape);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onEscape);
    };
  }, [searchOpen]);

  function go(next: Section) {
    const section = sections.find(([name]) => name === next);
    if (!section) return;
    setDialog(null);
    setSearchOpen(false);
    const hash = `#/${section[1]}`;
    if (searchOpen && window.location.hash === hash)
      searchTrigger.current?.focus({ preventScroll: true });
    if (window.location.hash !== hash) window.location.hash = hash;
  }

  function closeSearch(restoreFocus: boolean) {
    setSearchOpen(false);
    if (restoreFocus) searchTrigger.current?.focus({ preventScroll: true });
  }

  function closeNavigation() {
    if (dialog !== 'navigation' || drawerClosingRef.current) return;
    drawerClosingRef.current = true;
    setDrawerClosing(true);
    const reduced = motion.reduced || window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    drawerCloseTimer.current = window.setTimeout(
      () => {
        setDialog(null);
        setDrawerClosing(false);
        drawerClosingRef.current = false;
        drawerCloseTimer.current = null;
      },
      reduced ? 0 : 240
    );
  }

  function openNavigation() {
    if (dialog === 'navigation') {
      closeNavigation();
      return;
    }
    if (drawerCloseTimer.current !== null) window.clearTimeout(drawerCloseTimer.current);
    drawerClosingRef.current = false;
    setDrawerClosing(false);
    setSearchOpen(false);
    setNotificationsOpen(false);
    setDialog('navigation');
  }

  function toggleSearch() {
    if (searchOpen) {
      closeSearch(true);
      return;
    }
    setDialog(null);
    setNotificationsOpen(false);
    setQuery('');
    setActiveResult(0);
    setSearchOpen(true);
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

  return (
    <div
      className={`th-workspace h-dvh min-h-0 overflow-hidden bg-[#08090b] p-3 text-[#ececf0] selection:bg-white/25 ${motion.reduced ? 'th-stop-motion' : ''}`}
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
      <div className="relative mx-auto flex h-full min-h-0 min-w-0 max-w-[1550px] overflow-hidden rounded-[24px] border border-white/[.1] bg-[#101114] shadow-[0_40px_130px_rgba(0,0,0,.5)]">
        <aside className="z-30 hidden w-[237px] shrink-0 flex-col overflow-hidden border-r border-white/[.08] bg-[#131417] p-4 lg:flex">
          <div className="mb-10 flex shrink-0 items-center gap-3 px-2 pt-3">
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
          <div className="mb-3 shrink-0 px-3 text-[10px] font-semibold tracking-[.18em] text-[#a4a5af]">
            WORKSPACE
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            <WorkspaceNavigation page={page} onNavigate={() => setDialog(null)} />
          </div>
          <div className="mt-4 shrink-0 rounded-2xl border border-white/[.08] bg-[#1c1d21] p-3">
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
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <header className="relative z-10 flex min-h-[70px] shrink-0 items-center justify-between gap-1 border-b border-white/[.08] px-8 max-md:px-3">
            <div className="flex min-w-0 items-center gap-2 sm:gap-3">
              <button
                type="button"
                data-testid="navigation-trigger"
                className="grid min-h-10 min-w-10 place-items-center rounded-lg hover:bg-white/10 lg:hidden"
                aria-label={dialog === 'navigation' ? 'Close navigation' : 'Open navigation'}
                aria-expanded={dialog === 'navigation'}
                aria-controls="workspace-navigation"
                onClick={openNavigation}
              >
                <MenuGlyph open={dialog === 'navigation'} />
              </button>
              <span className="hidden text-xs text-[#a4a5af] sm:inline">Workspace</span>
              <span aria-hidden="true" className="hidden text-[#777985] sm:inline">
                /
              </span>
              <span className="truncate text-xs font-medium">{page}</span>
            </div>
            <div className="flex shrink-0 items-center gap-1 sm:gap-3">
              <Chip>WEB ALPHA</Chip>
              <div
                ref={searchAnchor}
                className="relative h-10 w-10 shrink-0"
                onBlur={event => {
                  if (
                    event.relatedTarget instanceof Node &&
                    !event.currentTarget.contains(event.relatedTarget)
                  )
                    setSearchOpen(false);
                }}
              >
                <input
                  ref={searchInput}
                  role="combobox"
                  aria-label="Jump to page"
                  aria-autocomplete="list"
                  aria-expanded={searchOpen}
                  aria-controls="workspace-search-results"
                  aria-activedescendant={
                    searchOpen && matching.length
                      ? `workspace-search-option-${matching[activeResult]?.[1] ?? matching[0][1]}`
                      : undefined
                  }
                  aria-hidden={!searchOpen}
                  tabIndex={searchOpen ? 0 : -1}
                  className={`th-search-input ${searchOpen ? 'is-open' : ''}`}
                  placeholder="Search pages…"
                  value={query}
                  onChange={event => {
                    setQuery(event.target.value);
                    setActiveResult(0);
                  }}
                  onKeyDown={event => {
                    if (event.nativeEvent.isComposing) return;
                    if (event.key === 'ArrowDown' && matching.length) {
                      event.preventDefault();
                      setActiveResult(index => (index + 1) % matching.length);
                    } else if (event.key === 'ArrowUp' && matching.length) {
                      event.preventDefault();
                      setActiveResult(index => (index - 1 + matching.length) % matching.length);
                    } else if (event.key === 'Enter' && matching.length) {
                      event.preventDefault();
                      go(matching[activeResult]?.[0] ?? matching[0][0]);
                    }
                  }}
                />
                {searchOpen && query && (
                  <button
                    type="button"
                    aria-label="Clear search"
                    className="absolute right-10 top-0 z-20 grid h-10 w-8 place-items-center rounded-lg text-sm text-[#a4a5af] hover:bg-white/[.07] hover:text-white"
                    onClick={() => {
                      setQuery('');
                      setActiveResult(0);
                      searchInput.current?.focus();
                    }}
                  >
                    ×
                  </button>
                )}
                <button
                  ref={searchTrigger}
                  type="button"
                  data-testid="search-trigger"
                  className="relative z-30 grid h-10 w-10 place-items-center rounded-lg text-[#b4b4bb] transition-colors duration-150 hover:bg-white/10"
                  aria-label={searchOpen ? 'Close search' : 'Search pages'}
                  aria-keyshortcuts="Control+k Meta+k"
                  aria-expanded={searchOpen}
                  aria-controls="workspace-search-results"
                  onClick={toggleSearch}
                >
                  <SearchGlyph open={searchOpen} />
                </button>
                <AnchoredSurface
                  id="workspace-search-surface"
                  open={searchOpen}
                  className="th-search-surface"
                >
                  <div
                    id="workspace-search-results"
                    role="listbox"
                    aria-label="Workspace pages"
                    className="max-h-[min(360px,calc(100dvh-110px))] overflow-y-auto p-1.5"
                  >
                    {matching.map(([name, slug], index) => (
                      <button
                        id={`workspace-search-option-${slug}`}
                        type="button"
                        role="option"
                        aria-selected={index === activeResult}
                        tabIndex={-1}
                        key={slug}
                        className={`block min-h-11 w-full rounded-lg px-3 py-3 text-left text-xs transition-colors duration-150 ${
                          index === activeResult
                            ? 'bg-white/10 text-white'
                            : 'text-[#c4c5cb] hover:bg-white/[.07]'
                        }`}
                        onMouseEnter={() => setActiveResult(index)}
                        onClick={() => go(name)}
                      >
                        {name}{' '}
                        <span aria-hidden="true" className="float-right">
                          ↗
                        </span>
                      </button>
                    ))}
                    {!matching.length && (
                      <p role="status" className={`p-3 ${muted}`}>
                        No matching pages.
                      </p>
                    )}
                  </div>
                </AnchoredSurface>
              </div>
              <NotificationPanel open={notificationsOpen} onOpenChange={setNotificationsOpen}>
                <Chip>UNAVAILABLE IN WEB v1</Chip>
                <p className={`mt-3 ${muted}`}>
                  The browser API does not expose notification history or unread counts.
                </p>
              </NotificationPanel>
            </div>
          </header>
          <main
            id="main-content"
            tabIndex={-1}
            ref={main}
            className="relative min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain px-9 py-8 outline-none max-md:px-4 max-md:py-6"
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
                    reduced={motion.reduced}
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
          close={closeNavigation}
          cls={`th-nav-dialog mr-auto ml-3 min-h-[70dvh] max-w-[275px] bg-[#131417] ${
            drawerClosing ? 'is-closing' : ''
          }`}
          id="workspace-navigation"
        >
          <WorkspaceNavigation page={page} drawer onNavigate={closeNavigation} />
        </Modal>
      )}
    </div>
  );
}
