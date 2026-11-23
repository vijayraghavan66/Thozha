import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { WorkspaceApi } from '../../api/client';
import { errorText } from '../../api/client';
import { Settings } from '../../pages/WorkspacePages';
import { useApprovals, useDevices, useTasks, taskHref, httpStatus } from '../../pages/controlState';
import { Approvals } from '../../pages/Approvals';
import { Chip, Crest, Loading, Modal, Notice } from '../ui';
import { AnchoredSurface, NotificationPanel, TravelingSurface } from '../LiquidSurface';
import { muted } from '../styles';
import {
  ConversationConfirmation,
  ConversationHistory,
  type ConversationAction,
} from '../../chat/ConversationActions';
import { conversationListKey } from '../../chat/updates';

const ConversationWorkspace = lazy(() => import('../../chat/ConversationWorkspace'));
const TradingLab = lazy(() => import('../../trading/TradingLab'));
const MemoryCenter = lazy(() => import('../../memory/MemoryCenter'));
const DeveloperStudio = lazy(() => import('../../pages/DeveloperStudio'));
const DeviceHub = lazy(() => import('../../pages/DeviceHub'));
const AutomationStudio = lazy(() => import('../../pages/AutomationStudio'));
const sections = [
  ['AI Workspace', 'ai-workspace', '✳'],
  ['Tasks', 'tasks', '◷'],
  ['Approvals', 'approvals', '✓'],
  ['Automations', 'automations', '◇'],
  ['Memory Center', 'memory-center', '◎'],
  ['Device Hub', 'device-hub', '▣'],
  ['Developer Studio', 'developer-studio', '⌘'],
  ['Trading Lab', 'trading-lab', '⌁'],
  ['Settings', 'settings', '⚙'],
] as const;
type Section = (typeof sections)[number][0];
const currentPage = (hash: string) =>
  sections.find(([, slug]) => hash.split('?')[0] === `#/${slug}`)?.[0] ?? 'AI Workspace';
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
  collapsed = false,
}: {
  page: Section;
  drawer?: boolean;
  onNavigate: () => void;
  collapsed?: boolean;
}) {
  const nav = useRef<HTMLElement>(null);
  return (
    <nav ref={nav} aria-label="Main navigation" className="relative space-y-1">
      <TravelingSurface container={nav} selection={page} />
      {sections.map(([name, slug, symbol], index) => (
        <div key={slug} className="relative z-[1]">
          {(index === 3 || index === 6 || index === 8) && !collapsed && (
            <div className="px-3 pb-2 pt-5 text-[10px] tracking-[.18em] text-[#a4a5af]">
              {index === 3 ? 'ORGANIZE' : index === 6 ? 'LAB & TOOLS' : 'PREFERENCES'}
            </div>
          )}
          <a
            href={`#/${slug}`}
            data-dialog-focus={drawer && index === 0 ? '' : undefined}
            title={collapsed ? name : undefined}
            aria-label={collapsed ? name : undefined}
            onClick={onNavigate}
            aria-current={page === name ? 'page' : undefined}
            className={`flex w-full items-center gap-3 rounded-xl border border-transparent px-3 py-2.5 text-left text-[12px] transition-colors duration-200 ease-out ${collapsed ? 'justify-center' : ''} ${
              page === name
                ? 'font-semibold text-white'
                : 'text-[#a0a2a9] hover:bg-white/[.06] hover:text-white'
            }`}
          >
            <span aria-hidden="true" className="w-5 text-center text-lg font-light">
              {symbol}
            </span>
            {!collapsed && <span>{name}</span>}
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
  const [hash, setHash] = useState(() => window.location.hash);
  const page = currentPage(hash);
  const taskParam = new URLSearchParams(hash.split('?')[1]).get('task');
  const taskId = taskParam && /^[0-9a-f]{32}$/.test(taskParam) ? taskParam : undefined;
  const [taskDraft, setTaskDraft] = useState<string>();
  const [visitedChat, setVisitedChat] = useState(
    () => currentPage(window.location.hash) === 'AI Workspace'
  );
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(
    () => !window.matchMedia('(max-width: 1023px)').matches
  );
  const [compact, setCompact] = useState(() => window.matchMedia('(max-width: 1023px)').matches);
  const [inspectorSlot, setInspectorSlot] = useState<HTMLDivElement | null>(null);
  const [dialog, setDialog] = useState<'navigation' | null>(null);
  const [conversationAction, setConversationAction] = useState<ConversationAction | null>(null);
  const [conversationNotice, setConversationNotice] = useState('');
  const [changedConversationId, setChangedConversationId] = useState<string>();
  const conversationFallback = useRef<HTMLElement | null>(null);
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
  const inspectorTrigger = useRef<HTMLButtonElement>(null);
  const inspectorPanel = useRef<HTMLElement>(null);
  const focusAfterNavigation = useRef(false);
  const drawerClosingRef = useRef(false);
  const drawerCloseTimer = useRef<number | null>(null);
  const matching = sections.filter(([name]) =>
    name.toLowerCase().includes(query.trim().toLowerCase())
  );
  const conversations = useQuery({
    queryKey: conversationListKey,
    queryFn: ({ signal }) => api.list(signal),
    enabled: !problem,
  });
  const tasks = useTasks(api, 'active', !problem && (page === 'AI Workspace' || page === 'Tasks'));
  const approvals = useApprovals(api, !problem && page === 'AI Workspace');
  const devices = useDevices(api, !problem && page === 'Approvals');
  const hasInspector = page === 'AI Workspace' || page === 'Tasks';
  const historyProps = {
    items: conversations.data?.items ?? [],
    selectedId:
      page === 'AI Workspace' ? new URLSearchParams(hash.split('?')[1]).get('conversation') : null,
    loading: conversations.isPending && !problem,
    error:
      !problem && conversations.isError
        ? errorText(conversations.error, 'load conversations')
        : undefined,
    connected: !problem,
    onAction: openConversationAction,
  };

  function openConversationAction(action: ConversationAction, fallback: HTMLElement | null = null) {
    conversationFallback.current = fallback;
    setConversationNotice('');
    setConversationAction(action);
  }

  function finishConversationAction(message: string) {
    const changedId = conversationAction?.conversation.id;
    const currentHash = window.location.hash;
    if (
      currentPage(currentHash) === 'AI Workspace' &&
      new URLSearchParams(currentHash.split('?')[1]).get('conversation') === changedId
    ) {
      window.history.replaceState(window.history.state, '', '#/ai-workspace');
      setHash(window.location.hash);
    }
    setChangedConversationId(changedId);
    setConversationNotice(message);
    setConversationAction(null);
    requestAnimationFrame(() => {
      if (document.activeElement !== document.body) return;
      const fallback = conversationFallback.current;
      (fallback?.isConnected ? fallback : main.current)?.focus({ preventScroll: true });
    });
  }

  useEffect(() => {
    function onNavigate() {
      const next = currentPage(window.location.hash);
      focusAfterNavigation.current = true;
      setHash(window.location.hash);
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
    const query = window.matchMedia('(max-width: 1023px)');
    function resize() {
      setCompact(query.matches);
      setInspectorOpen(!query.matches);
    }
    query.addEventListener('change', resize);
    return () => query.removeEventListener('change', resize);
  }, []);
  useEffect(() => {
    if (!hasInspector || !inspectorOpen) return;
    function onEscape(event: KeyboardEvent) {
      if (!compact || document.querySelector('dialog[open]')) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        setInspectorOpen(false);
        inspectorTrigger.current?.focus({ preventScroll: true });
      } else if (event.key === 'Tab') {
        const focusable = inspectorPanel.current?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), a[href], input:not(:disabled), summary'
        );
        if (!focusable?.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }
    if (compact) inspectorPanel.current?.querySelector<HTMLElement>('button')?.focus();
    document.addEventListener('keydown', onEscape);
    return () => document.removeEventListener('keydown', onEscape);
  }, [hasInspector, inspectorOpen, compact]);
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
      className={`th-workspace th-v4 h-dvh min-h-0 overflow-hidden bg-[#08090b] text-[#ececf0] selection:bg-white/25 ${sidebarCollapsed ? 'th-rail-collapsed' : ''} ${motion.reduced ? 'th-stop-motion' : ''}`}
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
      <div className="relative flex h-full min-h-0 min-w-0 overflow-hidden bg-[#101114]">
        <aside
          aria-label="Workspace sidebar"
          className="th-v4-rail z-30 hidden shrink-0 flex-col overflow-hidden border-r border-white/[.08] bg-[#131417] p-3 lg:flex"
        >
          <div className="mb-5 flex shrink-0 items-center gap-3 px-2 pt-3">
            <Crest />
            {!sidebarCollapsed && (
              <div>
                <div className="text-[21px] font-semibold tracking-[-.06em]">
                  thozha<span className="text-[#858690]">.</span>
                </div>
                <p className="text-[9px] font-semibold tracking-[.17em] text-[#a4a5af]">
                  PERSONAL WORKSPACE
                </p>
              </div>
            )}
          </div>
          <button
            type="button"
            className="th-v4-new"
            onClick={() => {
              window.location.hash = `#/ai-workspace?new=${crypto.randomUUID()}`;
            }}
            title={sidebarCollapsed ? 'New conversation' : undefined}
            aria-label="New conversation"
          >
            <span aria-hidden="true">＋</span>
            {!sidebarCollapsed && <span>New conversation</span>}
          </button>
          <div className="min-h-0 flex-1 overflow-y-auto">
            <WorkspaceNavigation
              page={page}
              onNavigate={() => setDialog(null)}
              collapsed={sidebarCollapsed}
            />
            {!sidebarCollapsed && <ConversationHistory {...historyProps} />}
          </div>
          <button
            type="button"
            className="th-v4-collapse"
            onClick={() => setSidebarCollapsed(value => !value)}
            aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-expanded={!sidebarCollapsed}
          >
            {sidebarCollapsed ? '›' : '‹  Collapse sidebar'}
          </button>
          <div className="mt-2 shrink-0 rounded-xl border border-white/[.08] bg-[#1c1d21] p-2">
            <div className="flex items-center gap-3">
              <span
                aria-hidden="true"
                className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-[#3a3b42] text-xs font-semibold"
              >
                {userId.slice(0, 1).toUpperCase()}
              </span>
              {!sidebarCollapsed && (
                <div className="min-w-0">
                  <p className="truncate text-xs font-medium">Personal workspace</p>
                  <p className="mt-1 text-[10px] text-[#a4a5af]">
                    {problem ? 'Connection uncertain' : 'Signed in · Web v1'}
                  </p>
                </div>
              )}
            </div>
          </div>
        </aside>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <header className="relative z-10 flex min-h-[62px] shrink-0 items-center justify-between gap-1 border-b border-white/[.08] px-6 max-md:px-3">
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
              <span className="hidden text-xs text-[#a4a5af] sm:inline">Thozha</span>
              <span aria-hidden="true" className="hidden text-[#777985] sm:inline">
                /
              </span>
              <span className="truncate text-xs font-medium">{page}</span>
            </div>
            <div className="flex shrink-0 items-center gap-1 sm:gap-3">
              {hasInspector && (
                <button
                  type="button"
                  ref={inspectorTrigger}
                  className="th-v4-inspector-toggle"
                  aria-label={inspectorOpen ? 'Close context panel' : 'Open context panel'}
                  aria-expanded={inspectorOpen}
                  onClick={() => setInspectorOpen(value => !value)}
                >
                  ◫ <span className="hidden sm:inline">Context</span>
                </button>
              )}
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
                <div
                  className="th-search-field"
                  data-open={searchOpen}
                  data-query={Boolean(query)}
                  inert={!searchOpen}
                  aria-hidden={!searchOpen}
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
                    className="th-search-input"
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
                  {query && (
                    <button
                      type="button"
                      aria-label="Clear search"
                      className="absolute right-10 top-0 grid h-10 w-8 place-items-center rounded-lg text-sm text-[#a4a5af] hover:bg-white/[.07] hover:text-white focus-visible:outline-offset-[-4px]"
                      onClick={() => {
                        setQuery('');
                        setActiveResult(0);
                        searchInput.current?.focus();
                      }}
                    >
                      ×
                    </button>
                  )}
                </div>
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
          <div className="th-v4-content-row flex min-h-0 min-w-0 flex-1">
            <main
              id="main-content"
              data-workspace={page}
              tabIndex={-1}
              ref={main}
              className={`relative min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain outline-none ${page === 'AI Workspace' ? 'th-v4-chat-main' : 'th-v4-page-main'}`}
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
                  {page === 'Trading Lab' && (
                    <Suspense fallback={<Loading label="Loading Trading Lab…" />}>
                      <TradingLab />
                    </Suspense>
                  )}
                  {page === 'Device Hub' && (
                    <Suspense fallback={<Loading label="Loading Device Hub…" />}>
                      <DeviceHub api={api} connected={!problem} />
                    </Suspense>
                  )}
                  {page === 'Automations' && (
                    <Suspense fallback={<Loading label="Loading Automations…" />}>
                      <AutomationStudio api={api} connected={!problem} />
                    </Suspense>
                  )}
                  {page === 'Memory Center' && (
                    <Suspense fallback={<Loading label="Loading Memory Center…" />}>
                      <MemoryCenter api={api} connected={!problem} />
                    </Suspense>
                  )}
                  {(page === 'Developer Studio' || page === 'Tasks') && (
                    <Suspense fallback={<Loading label="Loading Developer Studio…" />}>
                      <DeveloperStudio
                        api={api}
                        connected={!problem}
                        taskId={taskId}
                        draft={taskDraft}
                        clearDraft={() => setTaskDraft(undefined)}
                        view={page === 'Tasks' ? 'tasks' : 'developer'}
                      />
                    </Suspense>
                  )}
                  {page === 'Approvals' && (
                    <div className="th-v4-approvals-page">
                      <div className="th-v4-page-heading">
                        <p className="th-v4-eyebrow">REQUIRES YOUR DECISION</p>
                        <h1>Approvals</h1>
                        <p>
                          Review the exact operation, device, and expiry before deciding. Nothing is
                          approved implicitly.
                        </p>
                      </div>
                      <Approvals
                        api={api}
                        devices={devices.data?.devices ?? []}
                        connected={!problem}
                      />
                    </div>
                  )}
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
                      onReviewTask={objective => {
                        setTaskDraft(objective);
                        go('Tasks');
                      }}
                      inspector={inspectorSlot}
                      onManageConversation={openConversationAction}
                      changedConversationId={changedConversationId}
                    />
                  </Suspense>
                </div>
              )}
            </main>
            {hasInspector && (
              <aside
                ref={inspectorPanel}
                role={compact && inspectorOpen ? 'dialog' : undefined}
                aria-modal={compact && inspectorOpen ? true : undefined}
                aria-label="Context inspector"
                className={`th-v4-inspector ${inspectorOpen ? 'is-open' : ''}`}
                inert={!inspectorOpen}
              >
                <div className="th-v4-inspector-head">
                  <span>CONTEXT</span>
                  <button
                    type="button"
                    onClick={() => {
                      setInspectorOpen(false);
                      inspectorTrigger.current?.focus();
                    }}
                    aria-label="Close context panel"
                  >
                    ✕
                  </button>
                </div>
                <div className="th-v4-inspector-scroll">
                  {page === 'AI Workspace' && <div ref={setInspectorSlot} />}
                  <section className="th-v4-inspector-section">
                    <h2>Active tasks</h2>
                    {tasks.isPending && !problem && <p className={muted}>Checking supervisor…</p>}
                    {tasks.isError && (
                      <p className={muted}>
                        {httpStatus(tasks.error) === 403
                          ? 'Supervisor unavailable for this owner.'
                          : errorText(tasks.error, 'load tasks')}
                      </p>
                    )}
                    {tasks.data?.tasks.length === 0 && <p className={muted}>No active tasks.</p>}
                    {tasks.data?.tasks.slice(0, 4).map(item => (
                      <a
                        key={item.task_id}
                        href={taskHref(item.task_id)}
                        className="th-v4-inspector-link"
                      >
                        <span className="truncate">
                          {item.summary || `Task ${item.task_id.slice(0, 8)}`}
                        </span>
                        <small>{item.state.replace(/_/g, ' ')}</small>
                      </a>
                    ))}
                  </section>
                  {page === 'AI Workspace' && (
                    <section className="th-v4-inspector-section">
                      <h2>Pending approvals</h2>
                      {approvals.isPending && !problem && (
                        <p className={muted}>Checking approvals…</p>
                      )}
                      {approvals.isError && (
                        <p className={muted}>
                          {httpStatus(approvals.error) === 403
                            ? 'Approval access unavailable for this owner.'
                            : errorText(approvals.error, 'load approvals')}
                        </p>
                      )}
                      {approvals.data?.count === 0 && (
                        <p className={muted}>No pending approvals.</p>
                      )}
                      {approvals.data?.approvals.slice(0, 3).map(item => (
                        <a key={item.id} href="#/approvals" className="th-v4-inspector-link">
                          <span className="truncate">{item.operation}</span>
                          <small>Review required</small>
                        </a>
                      ))}
                    </section>
                  )}
                </div>
              </aside>
            )}
            {hasInspector && inspectorOpen && (
              <button
                className="th-v4-inspector-backdrop"
                type="button"
                aria-label="Close context panel"
                onClick={() => setInspectorOpen(false)}
              />
            )}
          </div>
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
          <button
            type="button"
            className="th-v4-new mb-3 w-full"
            onClick={() => {
              closeNavigation();
              window.location.hash = `#/ai-workspace?new=${crypto.randomUUID()}`;
            }}
          >
            ＋ New conversation
          </button>
          <WorkspaceNavigation page={page} drawer onNavigate={closeNavigation} />
          <ConversationHistory {...historyProps} onSelect={closeNavigation} />
          <p
            role="status"
            className={conversationNotice ? 'mt-3 text-xs text-[#c3c4c9]' : 'sr-only'}
          >
            {conversationNotice}
          </p>
        </Modal>
      )}
      {dialog !== 'navigation' && (
        <p role="status" className={conversationNotice ? 'th-conversation-notice' : 'sr-only'}>
          {conversationNotice}
        </p>
      )}
      {conversationAction && (
        <ConversationConfirmation
          api={api}
          connected={!problem}
          action={conversationAction}
          close={() => setConversationAction(null)}
          completed={finishConversationAction}
        />
      )}
    </div>
  );
}
