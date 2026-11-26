import { SessionGate } from './auth/SessionGate';
import { WorkspaceErrorBoundary } from './components/WorkspaceErrorBoundary';

export default function App() {
  return (
    <WorkspaceErrorBoundary>
      <SessionGate />
    </WorkspaceErrorBoundary>
  );
}
