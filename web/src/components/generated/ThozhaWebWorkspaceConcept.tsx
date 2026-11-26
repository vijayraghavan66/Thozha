import {
  lazy,
  Suspense,
  useId,
  useLayoutEffect,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { flushSync } from 'react-dom';
import { useQuery } from '@tanstack/react-query';
import type { Conversation, WorkspaceApi } from '../../api/client';
import { errorText } from '../../api/client';
import { Settings } from '../../pages/WorkspacePages';
import { useApprovals, useDevices, useTasks, taskHref, httpStatus } from '../../pages/controlState';
import { Approvals } from '../../pages/Approvals';
import { Chip, Head, Loading, Modal, Notice } from '../ui';
import { LivingPulseMark } from '../../auth/LivingPulseStartup';
import { AnchoredSurface, NotificationPanel, TravelingSurface } from '../LiquidSurface';
import { muted } from '../styles';
import { WorkspaceIcon } from '../WorkspaceIcon';
import { motionAllowed, resolveIdentity, type IdentityState } from '../motion';
import {
  ConversationConfirmation,
  ConversationHistory,
  ConversationRenameDialog,
  type ConversationAction,
  type ConversationMenuAction,
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
      {sections.map(([name, slug], index) => (
        <div
          key={slug}
          className={`relative z-[1] ${!drawer && [3, 6, 8].includes(index) ? 'th-nav-group-start' : ''}`}
        >
          {(index === 3 || index === 6 || index === 8) && drawer && !collapsed && (
            <div className="px-3 pb-2 pt-5 text-[10px] tracking-[.18em] text-[#a4a5af]">
              {index === 3 ? 'ORGANIZE' : index === 6 ? 'LAB & TOOLS' : 'PREFERENCES'}
            </div>
          )}
          <a
            href={`#/${slug}`}
            data-dialog-focus={drawer && index === 0 ? '' : undefined}
            title={name === 'AI Workspace' ? 'Chats' : name}
            aria-label={name}
            onClick={onNavigate}
            aria-current={page === name ? 'page' : undefined}
            className={`flex w-full items-center gap-3 rounded-xl border border-transparent px-3 py-2.5 text-left text-[12px] transition-colors duration-200 ease-out ${collapsed ? 'justify-center px-1 py-2.5' : ''} ${
              page === name
                ? 'font-semibold text-white'
                : 'text-[#a0a2a9] hover:bg-white/[.06] hover:text-white'
            }`}
          >
            <WorkspaceIcon name={name} />
            <span className="th-nav-label">{name === 'AI Workspace' ? 'Chats' : name}</span>
          </a>
        </div>
      ))}
    </nav>
  );
}

function ProfileMenu({
  placement,
  userId,
  displayName,
  expiresAt,
  onSettings,
  logout,
}: {
  placement: 'rail' | 'header';
  userId: string;
  displayName: string;
  expiresAt: number;
  onSettings: () => void;
  logout: () => void;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const anchor = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const initial = Array.from(displayName)[0]?.toUpperCase() ?? 'V';

  useLayoutEffect(() => {
    if (open) menu.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
  }, [open]);
  useEffect(() => {
    if (!open) return;
    function dismiss(event: PointerEvent) {
      if (event.target instanceof Node && !anchor.current?.contains(event.target)) {
        if (anchor.current?.contains(document.activeElement))
          trigger.current?.focus({ preventScroll: true });
        setOpen(false);
      }
    }
    function escape(event: KeyboardEvent) {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      trigger.current?.focus({ preventScroll: true });
      setOpen(false);
    }
    document.addEventListener('pointerdown', dismiss, true);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', dismiss, true);
      document.removeEventListener('keydown', escape);
    };
  }, [open]);

  function onMenuKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    const items = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')
    );
    const current = items.indexOf(document.activeElement as HTMLButtonElement);
    let next = -1;
    if (event.key === 'ArrowDown') next = (current + 1) % items.length;
    if (event.key === 'ArrowUp') next = (current - 1 + items.length) % items.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = items.length - 1;
    if (next < 0 || !items.length) return;
    event.preventDefault();
    items[next].focus();
  }

  return (
    <div
      ref={anchor}
      className={`th-v4-profile-anchor th-v4-profile-${placement}`}
      onBlur={event => {
        if (
          event.relatedTarget instanceof Node &&
          !event.currentTarget.contains(event.relatedTarget)
        )
          setOpen(false);
      }}
    >
      <button
        ref={trigger}
        type="button"
        className="th-v4-profile-trigger"
        aria-label={open ? 'Close profile menu' : 'Open profile menu'}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={id}
        title={`${displayName} — profile and workspace`}
        onClick={() => setOpen(value => !value)}
      >
        <span className="th-profile-avatar" aria-hidden="true">
          {initial}
        </span>
        {placement === 'rail' && (
          <>
            <span className="th-profile-copy">
              <span>{displayName}</span>
              <span>Personal workspace</span>
            </span>
            <svg
              className="th-profile-indicator"
              aria-hidden="true"
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            >
              <path d="m8 10 4-4 4 4 M8 14l4 4 4-4" />
            </svg>
          </>
        )}
      </button>
      <AnchoredSurface id={id} open={open} className="th-profile-surface">
        <div
          ref={menu}
          role="menu"
          aria-label="Profile and workspace"
          className="p-3"
          onKeyDown={onMenuKeyDown}
        >
          <div className="flex items-center gap-3 border-b border-white/10 px-2 pb-3 pt-2">
            <span
              aria-hidden="true"
              className="grid size-10 shrink-0 place-items-center rounded-full bg-[#3a3b42] text-sm font-semibold"
            >
              {initial}
            </span>
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">{displayName}</p>
              <p className="mt-1 text-[11px] text-[#a4a5af]">Verified Web owner</p>
            </div>
          </div>
          <p className="px-2 pt-3 text-xs leading-5 text-[#a4a5af]">
            Verified owner: <span className="break-all">{userId}</span>
          </p>
          <p className="px-2 py-3 text-[11px] leading-5 text-[#a4a5af]">
            Session expires {new Date(expiresAt * 1000).toLocaleString()}
          </p>
          <button
            type="button"
            role="menuitem"
            tabIndex={-1}
            className="flex min-h-10 w-full items-center gap-3 rounded-lg px-2 text-left text-xs text-[#d3d4d8] hover:bg-white/[.08] focus-visible:bg-white/[.08]"
            onClick={() => {
              setOpen(false);
              onSettings();
            }}
          >
            <WorkspaceIcon name="Settings" />
            Preferences
          </button>
          <button
            type="button"
            role="menuitem"
            tabIndex={-1}
            className="flex min-h-10 w-full items-center gap-3 rounded-lg px-2 text-left text-xs text-[#d3d4d8] hover:bg-white/[.08] focus-visible:bg-white/[.08]"
            onClick={() => {
              setOpen(false);
              logout();
            }}
          >
            <span aria-hidden="true">↪</span>
            Sign out
          </button>
        </div>
      </AnchoredSurface>
    </div>
  );
}

function MobileNavigation({
  page,
  onChats,
  onNavigate,
  onExplore,
}: {
  page: Section;
  onChats: () => void;
  onNavigate: (route: Section) => void;
  onExplore: () => void;
}) {
  const nav = useRef<HTMLElement>(null);
  const explore = !['AI Workspace', 'Tasks', 'Approvals'].includes(page);
  return (
    <nav ref={nav} aria-label="Mobile navigation" className="th-v4-mobile-nav">
      <TravelingSurface container={nav} selection={page} />
      {(
        [
          ['Chats', 'AI Workspace', '✳'],
          ['Tasks', 'Tasks', '◷'],
          ['Approvals', 'Approvals', '✓'],
          ['Explore', 'Explore', '◇'],
        ] as const
      ).map(([label, destination]) => {
        const active = destination === 'Explore' ? explore : page === destination;
        return (
          <button
            key={destination}
            type="button"
            aria-current={active ? 'page' : undefined}
            className="th-v4-mobile-tab"
            onClick={() => {
              if (destination === 'Explore') onExplore();
              else if (destination === 'AI Workspace') onChats();
              else onNavigate(destination);
            }}
          >
            <WorkspaceIcon name={destination} />
            {label}
          </button>
        );
      })}
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
  replayIntroduction,
}: {
  api: WorkspaceApi;
  userId: string;
  expiresAt: number;
  problem?: string;
  checkedAt: number;
  logout: () => void;
  checkSession: () => void;
  replayIntroduction: () => void;
}) {
  const [hash, setHash] = useState(() => window.location.hash);
  const page = currentPage(hash);
  const taskParam = new URLSearchParams(hash.split('?')[1]).get('task');
  const taskId = taskParam && /^[0-9a-f]{32}$/.test(taskParam) ? taskParam : undefined;
  const [taskDraft, setTaskDraft] = useState<string>();
  const [visitedChat, setVisitedChat] = useState(
    () => currentPage(window.location.hash) === 'AI Workspace'
  );
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try {
      return localStorage.getItem('thozha-web.navigation.v1') === 'collapsed';
    } catch {
      return false;
    }
  });
  const [navigationProblem, setNavigationProblem] = useState('');
  const [historyOpen, setHistoryOpen] = useState(false);
  const [mobileHistory, setMobileHistory] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [compact, setCompact] = useState(() => window.matchMedia('(max-width: 767px)').matches);
  const [inspectorSlot, setInspectorSlot] = useState<HTMLDivElement | null>(null);
  const [chatTitleSlot, setChatTitleSlot] = useState<HTMLDivElement | null>(null);
  const [dialog, setDialog] = useState<'navigation' | null>(null);
  const [conversationAction, setConversationAction] = useState<ConversationAction | null>(null);
  const [conversationRename, setConversationRename] = useState<Conversation | null>(null);
  const [conversationNotice, setConversationNotice] = useState('');
  const [changedConversationId, setChangedConversationId] = useState<string>();
  const conversationFallback = useRef<HTMLElement | null>(null);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [drawerClosing, setDrawerClosing] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeResult, setActiveResult] = useState(0);
  const [motion, setMotion] = useState(motionPreference);
  const [chatActivity, setChatActivity] = useState<IdentityState>('idle');
  const routeTransition = useRef<ViewTransition | null>(null);
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
  const historyVisible = page === 'AI Workspace' && (compact ? mobileHistory : historyOpen);
  useEffect(() => {
    if (!historyVisible || compact) return;
    function dismiss(event: PointerEvent | KeyboardEvent) {
      if (event instanceof KeyboardEvent) {
        if (event.key !== 'Escape' || document.querySelector('dialog[open]')) return;
        if (document.querySelector('.th-history-row[data-actions="true"]')) return;
        setHistoryOpen(false);
        document.querySelector<HTMLButtonElement>('.th-v4-history-restore')?.focus();
      } else if (
        event.target instanceof Element &&
        !event.target.closest('.th-v4-history-pane, .th-v4-history-restore, dialog')
      ) {
        setHistoryOpen(false);
      }
    }
    document.addEventListener('pointerdown', dismiss);
    document.addEventListener('keydown', dismiss);
    return () => {
      document.removeEventListener('pointerdown', dismiss);
      document.removeEventListener('keydown', dismiss);
    };
  }, [historyVisible, compact]);
  const conversations = useQuery({
    queryKey: conversationListKey,
    queryFn: ({ signal }) => api.list(signal),
    enabled: !problem,
  });
  const tasks = useTasks(api, 'active', !problem && (page === 'AI Workspace' || page === 'Tasks'));
  const approvals = useApprovals(api, !problem && page === 'AI Workspace');
  const devices = useDevices(api, !problem && page === 'Approvals');
  const supervisorIdentity: IdentityState = problem
    ? 'failed'
    : approvals.data?.approvals.some(item => item.status === 'PENDING') ||
        tasks.data?.tasks.some(item => item.state === 'WAITING_APPROVAL')
      ? 'approval'
      : tasks.data?.tasks.some(item =>
            ['CREATED', 'PLANNING', 'RUNNING', 'VERIFYING'].includes(item.state)
          )
        ? 'executing'
        : 'idle';
  const identity = resolveIdentity(
    page === 'AI Workspace' ? chatActivity : 'idle',
    supervisorIdentity
  );
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
    onAction: openHistoryAction,
    onNew: startNewConversation,
    onSelect: closeMobileHistory,
  };

  function openConversationAction(action: ConversationAction, fallback: HTMLElement | null = null) {
    conversationFallback.current = fallback;
    setConversationNotice('');
    setConversationRename(null);
    setConversationAction(action);
  }

  function openHistoryAction(
    action: ConversationMenuAction,
    conversation: Conversation,
    fallback: HTMLElement | null = null
  ) {
    conversationFallback.current = fallback;
    setConversationNotice('');
    if (action === 'rename') {
      setConversationAction(null);
      setConversationRename(conversation);
    } else {
      setConversationRename(null);
      openConversationAction({ type: action, conversation }, fallback);
    }
  }

  function finishConversationAction(message: string, preserveSelection = false) {
    const changedId = conversationAction?.conversation.id ?? conversationRename?.id;
    const currentHash = window.location.hash;
    if (
      !preserveSelection &&
      currentPage(currentHash) === 'AI Workspace' &&
      new URLSearchParams(currentHash.split('?')[1]).get('conversation') === changedId
    ) {
      if (conversationAction?.type === 'delete') startNewConversation();
      else window.history.replaceState(window.history.state, '', '#/ai-workspace');
      setHash(window.location.hash);
    }
    setChangedConversationId(changedId);
    setConversationNotice(message);
    setConversationAction(null);
    setConversationRename(null);
    requestAnimationFrame(() => {
      const fallback = conversationFallback.current;
      const target =
        fallback?.isConnected && !fallback.closest('[inert], [hidden]')
          ? fallback
          : (document.querySelector<HTMLButtonElement>('.th-v4-history-restore') ?? main.current);
      target?.focus({ preventScroll: true });
    });
  }

  useEffect(() => {
    let previous = currentPage(window.location.hash);
    function onNavigate() {
      const next = currentPage(window.location.hash);
      const update = () => {
        focusAfterNavigation.current = true;
        setHash(window.location.hash);
        if (next === 'AI Workspace') setVisitedChat(true);
        else setMobileHistory(false);
        if (!drawerClosingRef.current) setDialog(null);
        setSearchOpen(false);
        setNotificationsOpen(false);
      };
      routeTransition.current?.skipTransition();
      if (next !== previous && document.startViewTransition && motionAllowed(main.current)) {
        const index = (name: Section) => sections.findIndex(([section]) => section === name);
        document.documentElement.style.setProperty(
          '--th-route-direction',
          index(next) > index(previous) ? '1' : '-1'
        );
        routeTransition.current = document.startViewTransition(() => flushSync(update));
      } else update();
      previous = next;
    }
    window.addEventListener('hashchange', onNavigate);
    return () => {
      window.removeEventListener('hashchange', onNavigate);
      routeTransition.current?.skipTransition();
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
    const root = main.current?.closest('.th-workspace');
    const visibility = () => root?.setAttribute('data-motion-paused', String(document.hidden));
    visibility();
    document.addEventListener('visibilitychange', visibility);
    return () => document.removeEventListener('visibilitychange', visibility);
  }, []);
  useEffect(() => {
    const preference = matchMedia('(prefers-reduced-motion: reduce)');
    const reduce = () => {
      if (!motion.reduced && !preference.matches) return;
      routeTransition.current?.skipTransition();
      main.current
        ?.closest('.th-workspace')
        ?.getAnimations({ subtree: true })
        .forEach(animation => animation.cancel());
    };
    reduce();
    preference.addEventListener('change', reduce);
    return () => preference.removeEventListener('change', reduce);
  }, [motion.reduced]);
  useEffect(() => {
    const query = window.matchMedia('(max-width: 767px)');
    function resize() {
      setCompact(query.matches);
      if (query.matches) setInspectorOpen(false);
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
    setMobileHistory(false);
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

  function startNewConversation() {
    closeMobileHistory();
    window.location.hash = `#/ai-workspace?new=${crypto.randomUUID()}`;
  }

  function closeMobileHistory() {
    if (document.activeElement?.closest('.th-v4-history-pane')) {
      if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
      requestAnimationFrame(() =>
        document
          .querySelector<HTMLButtonElement>('.th-v4-history-restore')
          ?.focus({ preventScroll: true })
      );
    }
    setMobileHistory(false);
    setHistoryOpen(false);
  }

  function toggleNavigation() {
    const next = !sidebarCollapsed;
    setSidebarCollapsed(next);
    try {
      localStorage.setItem('thozha-web.navigation.v1', next ? 'collapsed' : 'expanded');
      setNavigationProblem('');
    } catch {
      setNavigationProblem(
        'Navigation preferences could not be saved. This change lasts until reload.'
      );
    }
  }

  function openMobileChats() {
    setMobileHistory(true);
    if (page !== 'AI Workspace') window.location.hash = '#/ai-workspace';
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
      <div
        className={`th-v4-layout relative flex h-full min-h-0 min-w-0 overflow-hidden bg-[#101114] ${page !== 'AI Workspace' ? 'th-nonchat' : ''} ${mobileHistory ? 'th-mobile-history' : ''}`}
      >
        <aside
          aria-label="Primary navigation"
          className="th-v4-nav-rail z-30 hidden shrink-0 flex-col border-r border-white/[.08] bg-[#0d0e11] px-2 py-3 lg:flex"
        >
          <div className="th-nav-brand-row">
            <a
              href="#/ai-workspace"
              className="th-v4-brand mb-4 grid h-12 shrink-0 place-items-center rounded-xl"
              aria-label="Thozha conversations"
              title="Thozha"
            >
              <span data-pulse-slot className="th-pulse-slot">
                <LivingPulseMark state={identity} />
              </span>
            </a>
            <button
              type="button"
              className="th-nav-collapse"
              aria-label={sidebarCollapsed ? 'Expand navigation' : 'Collapse navigation'}
              aria-expanded={!sidebarCollapsed}
              title={sidebarCollapsed ? 'Expand navigation' : 'Collapse navigation'}
              onClick={toggleNavigation}
            >
              <WorkspaceIcon name="sidebar" />
            </button>
          </div>
          <div className="th-nav-new">
            <button
              type="button"
              className="th-v4-rail-new mb-3"
              onClick={startNewConversation}
              aria-label="New conversation"
              title="New conversation"
            >
              <WorkspaceIcon name="new" />
              <span className="th-nav-label">New chat</span>
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            <WorkspaceNavigation
              page={page}
              onNavigate={() => setDialog(null)}
              collapsed={sidebarCollapsed}
            />
          </div>
          <ProfileMenu
            placement="rail"
            userId={userId}
            displayName={userId}
            expiresAt={expiresAt}
            onSettings={() => go('Settings')}
            logout={logout}
          />
        </aside>
        <aside
          aria-label="Conversation sidebar"
          aria-hidden={!historyVisible}
          inert={!historyVisible}
          className={`th-v4-history-pane shrink-0 border-r border-white/[.08] bg-[#121316] ${historyVisible ? '' : 'is-hidden'} ${compact && mobileHistory ? 'is-mobile-active' : ''}`}
        >
          <div className="th-v4-history-inner">
            <div className="th-v4-history-heading">
              <h2>Conversations</h2>
              {!compact && (
                <button
                  type="button"
                  className="th-v4-history-toggle"
                  onClick={() => {
                    setHistoryOpen(false);
                    requestAnimationFrame(() =>
                      document
                        .querySelector<HTMLButtonElement>('[aria-label="Open conversations"]')
                        ?.focus()
                    );
                  }}
                  aria-label="Close conversations"
                  aria-expanded="true"
                >
                  ‹
                </button>
              )}
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              <ConversationHistory {...historyProps} />
            </div>
            <button
              type="button"
              className="th-v4-mobile-new"
              onClick={startNewConversation}
              aria-label="New conversation"
            >
              ＋ New conversation
            </button>
          </div>
        </aside>
        <div
          className="th-v4-main-shell flex min-h-0 min-w-0 flex-1 flex-col"
          aria-hidden={compact && mobileHistory}
          inert={compact && mobileHistory}
        >
          <header className="relative z-10 flex min-h-[62px] shrink-0 items-center justify-between gap-1 border-b border-white/[.08] px-6 max-md:px-3">
            <div className="flex min-w-0 items-center gap-2 sm:gap-3">
              {compact && (
                <span data-pulse-slot className="th-pulse-slot th-pulse-slot-mobile">
                  <LivingPulseMark state={identity} />
                </span>
              )}
              {page === 'AI Workspace' && (!compact || !mobileHistory) && (
                <button
                  type="button"
                  className="th-v4-history-restore"
                  aria-label={
                    compact
                      ? 'Back to conversations'
                      : historyVisible
                        ? 'Close conversations'
                        : 'Open conversations'
                  }
                  aria-expanded={historyVisible}
                  onClick={() => {
                    if (compact) {
                      openMobileChats();
                    } else {
                      setHistoryOpen(value => !value);
                    }
                  }}
                >
                  <WorkspaceIcon name="history" />
                  <span className="hidden sm:inline">History</span>
                </button>
              )}
              {page === 'AI Workspace' ? (
                <div ref={setChatTitleSlot} className="min-w-0" />
              ) : (
                <>
                  <span className="hidden text-xs text-[#a4a5af] sm:inline">Thozha</span>
                  <span aria-hidden="true" className="hidden text-[#777985] sm:inline">
                    /
                  </span>
                  <span className="truncate text-xs font-medium">{page}</span>
                </>
              )}
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
                  <WorkspaceIcon name="context" /> <span className="hidden sm:inline">Context</span>
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
              <ProfileMenu
                placement="header"
                userId={userId}
                displayName={userId}
                expiresAt={expiresAt}
                onSettings={() => go('Settings')}
                logout={logout}
              />
            </div>
          </header>
          <div className="th-v4-content-row flex min-h-0 min-w-0 flex-1">
            <main
              id="main-content"
              data-workspace={page}
              tabIndex={-1}
              ref={main}
              className={`relative min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain outline-none ${page === 'AI Workspace' ? 'th-v4-chat-main' : 'th-v4-page-main'} ${page === 'Tasks' ? 'th-tasks-main' : ''}`}
            >
              {navigationProblem && <Notice error>{navigationProblem}</Notice>}
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
                      <Head
                        label="REQUIRES YOUR DECISION"
                        title="Approvals"
                        desc="Review the exact operation, device, and expiry before deciding. Nothing is approved implicitly."
                      />
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
                      replayIntroduction={replayIntroduction}
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
                      onActivityChange={setChatActivity}
                      workspaceIdentity={supervisorIdentity}
                      onReviewTask={objective => {
                        setTaskDraft(objective);
                        go('Tasks');
                      }}
                      inspector={inspectorSlot}
                      titleSlot={chatTitleSlot}
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
        <MobileNavigation
          page={page}
          onChats={openMobileChats}
          onNavigate={go}
          onExplore={openNavigation}
        />
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
          <WorkspaceNavigation
            page={page}
            drawer
            onNavigate={() => {
              setMobileHistory(false);
              closeNavigation();
            }}
          />
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
      {conversationRename && (
        <ConversationRenameDialog
          api={api}
          connected={!problem}
          conversation={conversationRename}
          close={() => setConversationRename(null)}
          completed={message => finishConversationAction(message, true)}
        />
      )}
    </div>
  );
}
