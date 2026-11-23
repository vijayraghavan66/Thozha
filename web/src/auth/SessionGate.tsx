import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  createWebApi,
  errorText,
  isUnauthorized,
  mayHaveBeenAccepted,
  type LoginInput,
  type WebApi,
  type WebSession,
} from '../api/client';
import { Chip, Crest, Loading, Notice, Panel } from '../components/ui';
import { WaterSurface } from '../components/WaterSurface';
import { field, muted, outline, solid } from '../components/styles';
import { ThozhaWebWorkspaceConcept } from '../components/generated/ThozhaWebWorkspaceConcept';

type Ready = {
  kind: 'ready';
  session: WebSession;
  version: number;
  checkedAt: number;
  problem?: string;
};
type State =
  | { kind: 'checking' | 'signing-in' | 'signing-out' }
  | { kind: 'signed-out'; message?: string }
  | { kind: 'unavailable'; message: string }
  | { kind: 'logout-error'; session: WebSession; message: string }
  | Ready;

const webApi = createWebApi();

function SignedIn({
  state,
  backend,
  onUnauthorized,
  logout,
  check,
}: {
  state: Ready;
  backend: WebApi;
  onUnauthorized: (version: number) => void;
  logout: () => void;
  check: () => void;
}) {
  const [cache] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 15_000,
            retry: (attempt, error) => attempt < 1 && mayHaveBeenAccepted(error),
          },
          mutations: { retry: false },
        },
      })
  );
  const [api] = useState(() =>
    backend.authorized(state.session.csrf_token, () => onUnauthorized(state.version))
  );
  return (
    <QueryClientProvider client={cache}>
      <ThozhaWebWorkspaceConcept
        api={api}
        userId={state.session.user_id}
        expiresAt={state.session.expires_at}
        problem={state.problem}
        checkedAt={state.checkedAt}
        logout={logout}
        checkSession={check}
      />
    </QueryClientProvider>
  );
}

export function SessionGate({ backend = webApi }: { backend?: WebApi }) {
  const [state, setState] = useState<State>({ kind: 'checking' });
  const current = useRef<State>(state);
  const request = useRef<AbortController | null>(null);
  const version = useRef(0);
  const commit = useCallback((next: State) => {
    current.current = next;
    setState(next);
  }, []);

  const verify = useCallback(async () => {
    const previous = current.current;
    if (
      request.current ||
      previous.kind === 'signing-in' ||
      previous.kind === 'signing-out' ||
      previous.kind === 'logout-error'
    )
      return;
    const controller = new AbortController();
    request.current = controller;
    if (previous.kind !== 'ready') commit({ kind: 'checking' });
    try {
      const session = await backend.session(controller.signal);
      if (controller.signal.aborted) return;
      const same =
        previous.kind === 'ready' &&
        previous.session.user_id === session.user_id &&
        previous.session.csrf_token === session.csrf_token &&
        previous.session.expires_at === session.expires_at;
      commit({
        kind: 'ready',
        session,
        checkedAt: Date.now(),
        version: same ? previous.version : ++version.current,
      });
    } catch (error) {
      if (controller.signal.aborted) return;
      if (isUnauthorized(error)) {
        commit({
          kind: 'signed-out',
          message:
            previous.kind === 'ready' ? 'Your session ended. Sign in to continue.' : undefined,
        });
      } else {
        const message = errorText(error, 'verify your session');
        commit(
          previous.kind === 'ready'
            ? { ...previous, problem: message }
            : { kind: 'unavailable', message }
        );
      }
    } finally {
      if (request.current === controller) request.current = null;
    }
  }, [backend, commit]);

  const invalidate = useCallback(
    (expected: number) => {
      const active = current.current;
      if (active.kind !== 'ready' || active.version !== expected) return;
      request.current?.abort();
      request.current = null;
      commit({ kind: 'signed-out', message: 'Your session ended. Sign in to continue.' });
    },
    [commit]
  );

  useEffect(() => {
    void verify();
    const reconnect = () => {
      if (['ready', 'unavailable'].includes(current.current.kind)) void verify();
    };
    const onVisible = () => {
      if (document.visibilityState === 'visible') reconnect();
    };
    const onRestore = (event: PageTransitionEvent) => {
      if (event.persisted) reconnect();
    };
    const onOffline = () => {
      const active = current.current;
      if (active.kind === 'ready')
        commit({
          ...active,
          problem: 'You are offline. Showing previously loaded workspace data.',
        });
    };
    window.addEventListener('online', reconnect);
    window.addEventListener('offline', onOffline);
    window.addEventListener('pageshow', onRestore);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      request.current?.abort();
      request.current = null;
      window.removeEventListener('online', reconnect);
      window.removeEventListener('offline', onOffline);
      window.removeEventListener('pageshow', onRestore);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [commit, verify]);

  const activeVersion = state.kind === 'ready' ? state.version : null;
  const expiresAt = state.kind === 'ready' ? state.session.expires_at : null;
  useEffect(() => {
    if (activeVersion === null || expiresAt === null) return;
    const timer = setTimeout(
      () => invalidate(activeVersion),
      Math.max(0, expiresAt * 1000 - Date.now())
    );
    return () => clearTimeout(timer);
  }, [activeVersion, expiresAt, invalidate]);

  async function signIn(input: LoginInput) {
    if (current.current.kind !== 'signed-out' || request.current) return;
    const controller = new AbortController();
    request.current = controller;
    commit({ kind: 'signing-in' });
    try {
      await backend.login(input, controller.signal);
      // Confirm the browser actually accepted the cookie before showing private screens.
      const session = await backend.session(controller.signal).catch(error => {
        if (isUnauthorized(error)) throw new Error('cookie-not-accepted');
        throw error;
      });
      if (!controller.signal.aborted)
        commit({ kind: 'ready', session, checkedAt: Date.now(), version: ++version.current });
    } catch (error) {
      if (controller.signal.aborted) return;
      const message = isUnauthorized(error)
        ? 'The workspace ID or password is incorrect.'
        : error instanceof Error && error.message === 'cookie-not-accepted'
          ? 'The browser did not keep the session cookie. Check the workspace origin and your cookie settings.'
          : errorText(error, 'sign in');
      commit({ kind: 'signed-out', message });
    } finally {
      if (request.current === controller) request.current = null;
    }
  }

  async function logout() {
    const active = current.current;
    if (active.kind !== 'ready' && active.kind !== 'logout-error') return;
    request.current?.abort();
    request.current = null;
    commit({ kind: 'signing-out' });
    let session = active.session;
    try {
      if (active.kind === 'logout-error') session = await backend.session();
      await backend.authorized(session.csrf_token, () => {}).logout();
      commit({ kind: 'signed-out' });
    } catch (error) {
      if (isUnauthorized(error)) commit({ kind: 'signed-out' });
      else commit({ kind: 'logout-error', session, message: errorText(error, 'sign out') });
    }
  }

  if (state.kind === 'ready') {
    return (
      <SignedIn
        key={state.version}
        state={state}
        backend={backend}
        onUnauthorized={invalidate}
        logout={() => void logout()}
        check={() => void verify()}
      />
    );
  }

  function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const password = event.currentTarget.querySelector<HTMLInputElement>('input[name="password"]');
    if (password) password.value = '';
    void signIn({
      username: String(values.get('username') ?? '').trim(),
      password: String(values.get('password') ?? ''),
    });
  }

  return (
    <main className="th-workspace grid min-h-dvh items-center bg-[#08090b] p-4 text-[#ececf0] md:p-10">
      <div className="th-enter mx-auto grid w-full max-w-[960px] overflow-hidden rounded-[24px] border border-white/10 bg-[#101114] shadow-[0_40px_130px_rgba(0,0,0,.5)] md:grid-cols-[1.1fr_1fr]">
        <div className="relative overflow-hidden bg-[#202126] p-8 md:p-10">
          <WaterSurface quiet />
          <div className="relative">
            <Crest />
            <p className="mt-8 text-[10px] font-semibold tracking-[.25em] text-[#c4c5cb]">
              THOZHA / WEB ALPHA
            </p>
            <p className="mt-4 text-3xl font-medium leading-tight tracking-[-.055em] md:text-[42px]">
              Intelligence,
              <br />
              <span className="text-[#aeb0b9]">without the noise.</span>
            </p>
            <p className={`mt-4 ${muted}`}>Your private, personal workspace.</p>
          </div>
        </div>
        <div className="p-5 md:p-8">
          <Panel cls="p-5 md:p-6">
            <Chip>OWNER SESSION</Chip>
            <h1 className="mb-4 mt-4 text-2xl font-semibold tracking-tight">Sign in to Thozha</h1>
            {(state.kind === 'checking' || state.kind === 'signing-out') && (
              <Loading
                label={state.kind === 'checking' ? 'Checking browser session…' : 'Signing out…'}
              />
            )}
            {state.kind === 'unavailable' && (
              <Notice error retry={() => void verify()}>
                {state.message}
              </Notice>
            )}
            {state.kind === 'logout-error' && (
              <>
                <Notice error>
                  Sign-out not confirmed. Workspace data is hidden. {state.message}
                </Notice>
                <button type="button" className={outline + ' mt-4'} onClick={() => void logout()}>
                  Retry sign-out
                </button>
              </>
            )}
            {(state.kind === 'signed-out' || state.kind === 'signing-in') && (
              <form className="space-y-4" onSubmit={handleLogin}>
                {state.kind === 'signed-out' && state.message && (
                  <Notice error>{state.message}</Notice>
                )}
                <label className="block text-xs">
                  Workspace ID
                  <input
                    name="username"
                    className={field + ' mt-2'}
                    autoComplete="username"
                    maxLength={128}
                    required
                  />
                </label>
                <label className="block text-xs">
                  Password
                  <input
                    name="password"
                    type="password"
                    className={field + ' mt-2'}
                    autoComplete="current-password"
                    maxLength={1024}
                    required
                  />
                </label>
                <p className={muted}>
                  Use the browser owner credential configured on your Thozha host.
                </p>
                <button
                  type="submit"
                  className={solid + ' w-full'}
                  disabled={state.kind === 'signing-in'}
                >
                  {state.kind === 'signing-in' ? 'Signing in…' : 'Sign in'}
                </button>
              </form>
            )}
          </Panel>
        </div>
      </div>
    </main>
  );
}
