import { useState, type FormEvent } from 'react';
import { skipToken, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  errorText,
  mayHaveBeenAccepted,
  type MemoryInput,
  type WebMemory,
  type WorkspaceApi,
} from '../api/client';
import { Chip, Head, Loading, Modal, Notice, Panel } from '../components/ui';
import { LiquidCard } from '../components/LiquidSurface';
import { field, formatDate, muted, outline, solid } from '../components/styles';

const pageSize = 50;
const keys = ['memories'] as const;
const bytes = (value: string) => new TextEncoder().encode(value).length;
type SaveAttempt = { input: MemoryInput; key: string };
type Selected = { memory: WebMemory; deleting: boolean };

export default function MemoryCenter({
  api,
  connected,
}: {
  api: WorkspaceApi;
  connected: boolean;
}) {
  const cache = useQueryClient();
  const [searchDraft, setSearchDraft] = useState('');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [searchError, setSearchError] = useState('');
  const [selected, setSelected] = useState<Selected | null>(null);
  const [saveIssue, setSaveIssue] = useState<{ message: string; retry?: SaveAttempt }>();
  const [deleteError, setDeleteError] = useState('');
  const [notice, setNotice] = useState('');
  const [formVersion, setFormVersion] = useState(0);
  const list = useQuery({
    queryKey: [...keys, { search, offset }],
    queryFn: ({ signal }) =>
      search
        ? api.searchMemories(search, pageSize, signal)
        : api.listMemories(pageSize, offset, signal),
    enabled: connected,
  });
  const selectedId = selected?.memory.id;
  const detail = useQuery({
    queryKey: [...keys, 'item', selectedId],
    queryFn: selectedId ? ({ signal }) => api.getMemory(selectedId, signal) : skipToken,
    enabled: connected && Boolean(selectedId) && !selected?.deleting,
  });
  const save = useMutation({
    mutationFn: (attempt: SaveAttempt) => api.createMemory(attempt.input, attempt.key),
    onSuccess: () => {
      setFormVersion(version => version + 1);
      setSaveIssue(undefined);
      setNotice('Saved to this owner’s Web memory.');
      setSearch('');
      setSearchDraft('');
      setOffset(0);
      void cache.invalidateQueries({ queryKey: keys });
    },
    onError: (error, attempt) =>
      setSaveIssue({
        message: errorText(error, 'save memory'),
        retry: mayHaveBeenAccepted(error) ? attempt : undefined,
      }),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteMemory(id),
    onSuccess: (_result, id) => {
      setSelected(null);
      setNotice('Memory deleted from the host.');
      cache.removeQueries({ queryKey: [...keys, 'item', id] });
      if (list.data?.items.length === 1 && !search)
        setOffset(value => Math.max(0, value - pageSize));
      void cache.invalidateQueries({ queryKey: keys });
    },
    onError: error => setDeleteError(errorText(error, 'delete memory')),
  });
  const latest = detail.data;
  const canSave = connected && !save.isPending && !saveIssue?.retry;

  function saveMemory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSave) return;
    const data = new FormData(event.currentTarget);
    const content = String(data.get('content')).trim();
    const title = String(data.get('title')).trim();
    const tags = [
      ...new Set(
        String(data.get('tags'))
          .split(',')
          .map(tag => tag.trim())
          .filter(Boolean)
      ),
    ];
    if (
      !content ||
      bytes(content) > 16_384 ||
      bytes(title) > 160 ||
      tags.length > 16 ||
      tags.some(tag => bytes(tag) > 64)
    ) {
      setSaveIssue({
        message:
          'Enter content up to 16 KiB, a title up to 160 UTF-8 bytes, and up to 16 tags of 64 bytes each.',
      });
      return;
    }
    setNotice('');
    setSaveIssue(undefined);
    save.mutate({ input: { content, title: title || null, tags }, key: crypto.randomUUID() });
  }

  function lookUp(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = searchDraft.trim();
    if (bytes(query) > 256) {
      setSearchError('Search terms must be at most 256 UTF-8 bytes.');
      return;
    }
    setSearchError('');
    setSearch(query);
    setOffset(0);
  }

  return (
    <>
      <Head
        label="CONTEXT / PRIVACY"
        title="Memory Center"
        desc="Review and deliberately save your browser-owner memories."
        action={<Chip>OWNER SCOPED · WEB v1</Chip>}
      />
      <p role="status" className={notice ? `mb-4 ${muted}` : 'sr-only'}>
        {notice}
      </p>
      <div className="grid grid-cols-2 gap-4 max-lg:grid-cols-1">
        <Panel cls="h-fit min-w-0 p-6">
          <h2 className="font-semibold">Add a memory</h2>
          <p className={`mt-2 ${muted}`}>
            Entries persist on your Thozha host until deleted. They are separate from Android memory
            and are not automatically injected into chat.
          </p>
          <form key={formVersion} onSubmit={saveMemory} className="mt-4 space-y-4">
            <label className="block text-xs">
              Title (optional)
              <input
                name="title"
                className={field + ' mt-2'}
                maxLength={160}
                placeholder="Preferred editor"
                readOnly={!canSave}
              />
            </label>
            <label className="block text-xs">
              Detail
              <textarea
                name="content"
                className={field + ' mt-2'}
                rows={4}
                maxLength={16_384}
                placeholder="I prefer…"
                required
                readOnly={!canSave}
              />
            </label>
            <label className="block text-xs">
              Tags (comma-separated)
              <input
                name="tags"
                className={field + ' mt-2'}
                maxLength={1056}
                placeholder="preferences, editor"
                readOnly={!canSave}
              />
            </label>
            {saveIssue && <Notice error>{saveIssue.message}</Notice>}
            {saveIssue?.retry ? (
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  className={outline}
                  disabled={!connected || save.isPending}
                  onClick={() => {
                    if (saveIssue.retry) save.mutate(saveIssue.retry);
                  }}
                >
                  Retry save safely
                </button>
                <button
                  type="button"
                  className={outline}
                  disabled={save.isPending}
                  onClick={() => {
                    setSaveIssue(undefined);
                    setFormVersion(version => version + 1);
                    setNotice(
                      'Draft discarded. Review saved entries to check whether it was already accepted.'
                    );
                    void cache.invalidateQueries({ queryKey: keys });
                  }}
                >
                  Discard draft
                </button>
              </div>
            ) : (
              <button type="submit" className={solid} disabled={!canSave}>
                {save.isPending ? 'Saving…' : 'Save on Thozha host'}
              </button>
            )}
          </form>
        </Panel>
        <Panel cls="min-w-0 p-6">
          <div className="flex flex-wrap justify-between gap-2">
            <h2 className="font-semibold">Saved preferences and memories</h2>
            <Chip>SUBSTRING SEARCH</Chip>
          </div>
          <form onSubmit={lookUp} className="mt-4 space-y-2">
            <label className="block text-xs">
              Search saved memories
              <input
                type="search"
                className={field + ' mt-2'}
                value={searchDraft}
                maxLength={256}
                onChange={event => setSearchDraft(event.target.value)}
              />
            </label>
            <div className="flex flex-wrap gap-2">
              <button type="submit" className={outline} disabled={!connected}>
                Search
              </button>
              {search && (
                <button
                  type="button"
                  className={outline}
                  onClick={() => {
                    setSearchDraft('');
                    setSearch('');
                    setOffset(0);
                    setSearchError('');
                  }}
                >
                  Clear search
                </button>
              )}
            </div>
          </form>
          {searchError && (
            <div className="mt-3">
              <Notice error>{searchError}</Notice>
            </div>
          )}
          {list.isPending && (
            <Loading
              label={connected ? 'Loading saved memories…' : 'Reconnect to load saved memories.'}
            />
          )}
          {list.isError && (
            <div className="mt-4">
              <Notice error retry={connected ? () => void list.refetch() : undefined}>
                {errorText(list.error, 'load memory')}
              </Notice>
            </div>
          )}
          {list.isSuccess && !list.data.items.length && (
            <p className={`py-10 text-center ${muted}`}>
              {search ? 'No matching memories.' : 'No saved memories on this page.'}
            </p>
          )}
          <ul className="max-h-[480px] overflow-y-auto p-1">
            {list.data?.items.map(memory => (
              <li key={memory.id} className="border-b border-white/10 py-4 last:border-none">
                <LiquidCard
                  className="block w-full break-words rounded-lg px-2 py-1 text-left hover:bg-white/10"
                  onClick={() => {
                    setSelected({ memory, deleting: false });
                    setDeleteError('');
                  }}
                >
                  <span className="block text-xs font-semibold">{memory.title}</span>
                  <span className="mt-1 block truncate text-[11px] text-[#a4a5af]">
                    {memory.tags.join(' · ') || 'No tags'}
                  </span>
                </LiquidCard>
              </li>
            ))}
          </ul>
          {search && (
            <p className={muted}>
              Showing up to {pageSize} matches. Refine the search for a narrower result.
            </p>
          )}
          {!search && (offset > 0 || list.data?.items.length === pageSize) && (
            <div className="mt-4 flex flex-wrap justify-between gap-2">
              <button
                type="button"
                className={outline}
                disabled={offset === 0 || !connected}
                onClick={() => setOffset(value => value - pageSize)}
              >
                Previous
              </button>
              <button
                type="button"
                className={outline}
                disabled={
                  list.data?.items.length !== pageSize || !connected || offset + pageSize >= 5000
                }
                onClick={() => setOffset(value => value + pageSize)}
              >
                Next
              </button>
            </div>
          )}
        </Panel>
      </div>
      <Panel cls="mt-5 p-6">
        <h2 className="font-semibold">Retention and context</h2>
        <p className={`mt-2 ${muted}`}>
          Web Memory has no edit, automatic retention or semantic retrieval setting. To correct
          something, save a replacement and delete the old entry. Conversation history is kept
          separately.
        </p>
        <a className={outline + ' mt-3 inline-block'} href="#/ai-workspace">
          Manage conversation history
        </a>
      </Panel>
      {selected && (
        <Modal
          title={selected.deleting ? 'Delete memory' : 'Read memory'}
          locked={remove.isPending}
          close={() => setSelected(null)}
        >
          {selected.deleting ? (
            <>
              <p className={muted}>
                Permanently delete “{selected.memory.title}” from Web Memory? This does not delete
                conversation history.
              </p>
              {deleteError && (
                <div className="mt-3">
                  <Notice error>{deleteError}</Notice>
                </div>
              )}
              <div className="mt-4 flex flex-wrap justify-end gap-2">
                <button
                  type="button"
                  className={outline}
                  disabled={remove.isPending}
                  onClick={() => setSelected({ ...selected, deleting: false })}
                >
                  Back
                </button>
                <button
                  type="button"
                  className={solid}
                  disabled={!connected || remove.isPending}
                  onClick={() => remove.mutate(selected.memory.id)}
                >
                  Delete permanently
                </button>
              </div>
            </>
          ) : (
            <>
              {detail.isPending && (
                <Loading label={connected ? 'Loading entry…' : 'Reconnect to read this entry.'} />
              )}
              {detail.isError && (
                <Notice error retry={connected ? () => void detail.refetch() : undefined}>
                  {errorText(detail.error, 'read memory')}
                </Notice>
              )}
              {latest && (
                <>
                  <h3 className="break-words text-base font-semibold">{latest.title}</h3>
                  <p className="mt-2 text-[11px] text-[#a4a5af]">
                    Updated {formatDate(latest.updated_at)}
                  </p>
                  <div className="my-3 flex flex-wrap gap-2">
                    {latest.tags.map(tag => (
                      <Chip key={tag}>{tag}</Chip>
                    ))}
                  </div>
                  <p
                    className={`max-h-[40dvh] overflow-y-auto whitespace-pre-wrap break-words ${muted}`}
                  >
                    {latest.content}
                  </p>
                  <button
                    type="button"
                    className={outline + ' mt-4'}
                    disabled={!connected}
                    onClick={() => setSelected({ memory: latest, deleting: true })}
                  >
                    Delete memory
                  </button>
                </>
              )}
            </>
          )}
        </Modal>
      )}
    </>
  );
}
