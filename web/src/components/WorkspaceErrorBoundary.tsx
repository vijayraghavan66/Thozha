import { Component, type ReactNode } from 'react';
import { outline } from './styles';

export class WorkspaceErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <main className="th-workspace min-h-screen bg-[#08090b] p-8 text-[#ececf0]">
        <h1 className="text-2xl font-semibold">The workspace couldn’t render</h1>
        <p role="alert" className="my-4 text-sm text-[#a4a5af]">
          Reload to reconnect. Saved conversations are kept on the server.
        </p>
        <button type="button" className={outline} onClick={() => window.location.reload()}>
          Reload the workspace
        </button>
      </main>
    );
  }
}
