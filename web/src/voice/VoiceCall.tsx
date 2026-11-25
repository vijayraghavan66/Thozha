import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useQueryClient } from '@tanstack/react-query';
import type { WorkspaceApi } from '../api/client';
import { conversationListKey, detailKey } from '../chat/updates';
import { TurnDetector, splitSpeech, wav16, type CallState } from './turns';
import './voice.css';

type SinkContext = AudioContext & { setSinkId?: (id: string) => Promise<void> };
type Snapshot = {
  state: CallState;
  message: string;
  user: string;
  assistant: string;
  level: number;
  metrics: Record<string, number>;
  muted: boolean;
};
type Pending = { conversation: string; key: string; content: string; response?: string };
const storage = 'thozha.voice.pending.v1';
const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    if (signal.aborted) return reject(signal.reason);
    const abort = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', abort);
      resolve();
    }, ms);
    signal.addEventListener('abort', abort, { once: true });
  });

/** One call owns all capture, synthesis, playback, request and reconnect lifetimes. */
export class CallSession {
  snapshot: Snapshot = {
    state: 'connecting',
    message: 'Requesting microphone access…',
    user: '',
    assistant: '',
    level: 0,
    metrics: {},
    muted: false,
  };
  private context?: SinkContext;
  private media?: MediaStream;
  private source?: MediaStreamAudioSourceNode;
  private worklet?: AudioWorkletNode;
  private zero?: GainNode;
  private output?: AudioBufferSourceNode;
  private detector = new TurnDetector();
  private frames: Float32Array[] = [];
  private preroll: Float32Array[] = [];
  private life = new AbortController();
  private turn = new AbortController();
  private epoch = 0;
  private closed = false;
  private busy = false;
  private reconcilingCancellation = false;
  private voice = 'af_bella';
  private csrf = '';
  private pending?: Pending;
  private conversation: string | null;
  private outputTail: Promise<void> = Promise.resolve();
  private synthTail: Promise<void> = Promise.resolve();
  private synthAbort = new AbortController();
  private interruptedAt = 0;
  private speechEnd = 0;
  private firstAudio = false;
  private online = () => {
    this.patch({ message: 'Connection restored. Reconciling the same request…' });
  };
  constructor(
    private api: WorkspaceApi,
    conversation: string | null,
    private notify: (snapshot: Snapshot) => void,
    private changed: (id: string) => void
  ) {
    this.conversation = conversation;
  }
  private patch(patch: Partial<Snapshot>) {
    if (this.closed) return;
    this.snapshot = { ...this.snapshot, ...patch };
    this.notify(this.snapshot);
  }
  private state(state: CallState, message: string) {
    this.patch({ state: this.snapshot.muted ? 'muted' : state, message });
  }
  private async speech(path: string, body: object, signal = this.life.signal) {
    const response = await fetch(`/v1/web/voice/${path}`, {
      method: 'POST',
      credentials: 'same-origin',
      mode: 'same-origin',
      redirect: 'error',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Thozha-CSRF': this.csrf },
      body: JSON.stringify(body),
      signal,
    });
    if (!response.ok)
      throw new Error(
        response.status === 401 || response.status === 403
          ? 'Web session expired. End the call and sign in again.'
          : 'Local speech unavailable; text chat still works.'
      );
    return response.json();
  }
  async start() {
    try {
      // Called directly from a click so capture and audio playback have user activation.
      this.context = new AudioContext({ latencyHint: 'interactive' });
      await this.context.resume();
      this.media = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      });
      if (this.closed) {
        this.media.getTracks().forEach(track => track.stop());
        return;
      }
      this.media.getAudioTracks().forEach(track => {
        track.onended = () => this.end();
      });
      const auth = await fetch('/v1/web/session', {
        cache: 'no-store',
        credentials: 'same-origin',
        signal: this.life.signal,
      });
      if (!auth.ok) throw new Error('Sign in to Thozha before starting a voice call.');
      this.csrf = (await auth.json()).csrf_token;
      this.patch({ message: 'Warming local neural voice…' });
      let health = await this.speech('health', {});
      for (let i = 0; !health.ready && i < 90; i++) {
        await sleep(1000, this.life.signal);
        health = await this.speech('health', {});
      }
      if (!health.ready)
        throw new Error('Local Kokoro model could not warm up. Text chat remains available.');
      await this.context.audioWorklet.addModule('/voice-capture.js');
      if (this.closed) return;
      this.source = this.context.createMediaStreamSource(this.media);
      this.worklet = new AudioWorkletNode(this.context, 'thozha-capture');
      this.zero = this.context.createGain();
      this.zero.gain.value = 0;
      this.source.connect(this.worklet);
      this.worklet.connect(this.zero);
      this.zero.connect(this.context.destination);
      this.worklet.port.onmessage = event => this.frame(event.data);
      window.addEventListener('online', this.online);
      let saved: Pending | undefined;
      try {
        saved = JSON.parse(sessionStorage.getItem(storage) || 'null') || undefined;
      } catch {
        /* no saved attempt */
      }
      if (saved?.conversation && saved.key && saved.content) {
        this.pending = saved;
        this.conversation = saved.conversation;
        this.busy = true;
        this.patch({
          user: saved.content,
          message: 'Recovering saved conversation request; microphone is active.',
        });
        void this.recover(saved, this.epoch).catch(error => this.problem(error));
      } else this.state('listening', 'Microphone active · speak naturally');
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Microphone unavailable';
      this.end();
      this.notify({ ...this.snapshot, state: 'ended', message });
    }
  }
  private frame(data: Float32Array) {
    if (
      this.closed ||
      !this.context ||
      this.snapshot.muted ||
      this.snapshot.state === 'disconnected'
    )
      return;
    let sum = 0;
    for (const sample of data) sum += sample * sample;
    const level = Math.sqrt(sum / data.length);
    this.patch({ level: Math.min(1, level * 12) });
    const playback = !!this.output;
    // Do not cancel ASR on energy alone. Room noise is not a new semantic turn.
    if (this.busy && !playback) return;
    const wasSpeaking = this.detector.speaking;
    const event = this.detector.feed(
      level,
      (data.length * 1000) / this.context.sampleRate,
      playback
    );
    this.preroll.push(data);
    while (this.preroll.length > 12) this.preroll.shift();
    if (event === 'start') {
      if (this.busy || playback) this.interrupt();
      if (this.reconcilingCancellation) {
        this.detector.reset();
        return;
      }
      this.frames = [...this.preroll];
      this.state('listening', 'Hearing you…');
    } else if (wasSpeaking) this.frames.push(data);
    if (event === 'end' && this.frames.length && !(this.pending && !this.pending.response)) {
      const frames = this.frames;
      this.frames = [];
      this.preroll = [];
      this.speechEnd = performance.now() - 620;
      const samples = new Float32Array(frames.reduce((n, frame) => n + frame.length, 0));
      let offset = 0;
      for (const frame of frames) {
        samples.set(frame, offset);
        offset += frame.length;
      }
      void this.respond(wav16(samples, this.context.sampleRate));
    }
  }
  private persist() {
    if (this.pending) sessionStorage.setItem(storage, JSON.stringify(this.pending));
    else sessionStorage.removeItem(storage);
  }
  private current(epoch: number) {
    return !this.closed && this.epoch === epoch;
  }
  private async respond(audio: string) {
    const epoch = ++this.epoch;
    this.turn.abort();
    this.turn = new AbortController();
    this.busy = true;
    this.firstAudio = false;
    this.patch({ metrics: {}, user: '', assistant: '' });
    this.state('thinking', 'Transcribing locally…');
    try {
      const result = await this.speech('transcribe', { audio }, this.turn.signal);
      if (!this.current(epoch)) return;
      const text = String(result.text || '').trim();
      if (!text) {
        this.busy = false;
        this.state('listening', 'No speech recognized · try again');
        return;
      }
      this.patch({
        user: text,
        metrics: { transcript_ms: Math.round(performance.now() - this.speechEnd) },
      });
      if (!this.conversation) {
        // Never automatically retry an uncertain create: it has no idempotency contract.
        const created = await this.api.create({ title: text.slice(0, 80) });
        if (!this.current(epoch)) return;
        this.conversation = created.id;
        this.changed(created.id);
      }
      this.pending = { conversation: this.conversation, content: text, key: crypto.randomUUID() };
      this.persist();
      await this.run(this.pending, epoch);
    } catch (error) {
      if (this.current(epoch)) this.problem(error);
    }
  }
  private async run(attempt: Pending, epoch: number) {
    // The deployed Luna conversation stream cancels generation on disconnect.
    // Use durable submission by default; enable streaming only after the backend
    // provides a disconnect-independent generation lifetime.
    if (localStorage.getItem('thozha.voice.experimental-stream') !== '1') {
      this.state('thinking', 'Waiting for the authorized Thozha conversation…');
      const accepted = await this.api.submit(
        attempt.conversation,
        { content: attempt.content },
        attempt.key
      );
      if (!this.current(epoch)) return;
      attempt.response = accepted.response_id;
      this.persist();
      this.changed(attempt.conversation);
      this.patch({
        metrics: {
          ...this.snapshot.metrics,
          acceptance_ms: Math.round(performance.now() - this.speechEnd),
        },
      });
      return this.recover(attempt, epoch);
    }
    this.state('thinking', 'Waiting for the authorized Thozha conversation…');
    let remainder = '',
      full = '',
      spoken = false,
      mode = 'buffered';
    try {
      await this.api.stream(
        attempt.conversation,
        { content: attempt.content },
        attempt.key,
        this.turn.signal,
        event => {
          if (!this.current(epoch)) return;
          if (event.event === 'accepted') {
            attempt.response = event.data.response_id;
            this.persist();
            this.changed(attempt.conversation);
            this.patch({
              metrics: {
                ...this.snapshot.metrics,
                acceptance_ms: Math.round(performance.now() - this.speechEnd),
              },
            });
          } else if (event.event === 'stream_mode') mode = event.data.mode;
          else if (event.event === 'delta' && mode === 'engine_stream') {
            full += event.data.content;
            remainder += event.data.content;
            this.patch({ assistant: full });
            const split = splitSpeech(remainder);
            remainder = split.rest;
            for (const sentence of split.sentences) {
              spoken = true;
              this.enqueue(sentence, epoch);
            }
          }
        }
      );
    } catch {
      if (!this.current(epoch)) return;
      // Same durable key on every retry, including uncertain acceptance.
      return this.recover(attempt, epoch);
    }
    if (!this.current(epoch)) return;
    const detail = await this.api.get(attempt.conversation, this.turn.signal);
    const answer = detail.messages.find(row => row.id === attempt.response);
    this.changed(attempt.conversation);
    if (!answer || answer.status === 'running' || answer.status === 'queued')
      return this.recover(attempt, epoch);
    if (answer.status !== 'completed')
      throw new Error(`Response ${answer.status}. Speak again or use text chat.`);
    this.patch({
      assistant: answer.content,
      metrics: {
        ...this.snapshot.metrics,
        response_ms: Math.round(performance.now() - this.speechEnd),
      },
    });
    const split = splitSpeech(spoken ? remainder : answer.content, true);
    for (const sentence of split.sentences) this.enqueue(sentence, epoch);
    await this.complete(epoch);
  }
  private async recover(attempt: Pending, epoch: number) {
    let delay = 600;
    while (this.current(epoch)) {
      try {
        if (!attempt.response) {
          const accepted = await this.api.submit(
            attempt.conversation,
            { content: attempt.content },
            attempt.key
          );
          attempt.response = accepted.response_id;
          this.persist();
        }
        const detail = await this.api.get(attempt.conversation, this.turn.signal);
        const row = detail.messages.find(item => item.id === attempt.response);
        this.changed(attempt.conversation);
        if (row?.status === 'completed') {
          this.stopOutput();
          this.patch({
            assistant: row.content,
            metrics: {
              ...this.snapshot.metrics,
              response_ms: Math.round(performance.now() - this.speechEnd),
            },
          });
          for (const sentence of splitSpeech(row.content, true).sentences)
            this.enqueue(sentence, epoch);
          await this.complete(epoch);
          return;
        }
        if (row && !['running', 'queued'].includes(row.status))
          throw new Error(`Response ${row.status}`);
        this.state('thinking', 'Reconciling saved answer · no duplicate submission');
        delay = 600;
      } catch (error) {
        if (!this.current(epoch)) return;
        // Authentication and semantic errors must not loop forever.
        const status = (error as { status?: number }).status;
        if (status && status < 500 && status !== 429) throw error;
        if (error instanceof Error && error.message.startsWith('Response ')) throw error;
        this.state(
          'disconnected',
          'Connection lost · microphone active · recovering the same request'
        );
        delay = Math.min(8000, delay * 2);
      }
      await sleep(delay, this.turn.signal);
    }
  }
  private enqueue(sentence: string, epoch: number) {
    const signal = this.synthAbort.signal;
    const voice = this.voice;
    // Only one sentence synthesizes ahead. Playback overlaps synthesis of the next.
    this.synthTail = this.synthTail
      .then(async () => {
        if (!this.current(epoch) || signal.aborted) return;
        const row = await this.speech(
          'synthesize',
          { text: sentence.slice(0, 400), voice },
          signal
        );
        if (!this.current(epoch) || signal.aborted || !this.context) return;
        const bytes = Uint8Array.from(atob(row.audio), c => c.charCodeAt(0));
        const buffer = await this.context.decodeAudioData(bytes.buffer);
        // Hold at most one synthesized sentence ahead of the currently playing one.
        await this.outputTail;
        if (!this.current(epoch) || signal.aborted) return;
        this.outputTail = this.outputTail.then(async () => {
          if (!this.current(epoch) || signal.aborted || !this.context) return;
          this.state('speaking', 'Thozha speaking · you can interrupt');
          await new Promise<void>(resolve => {
            const source = this.context!.createBufferSource();
            source.buffer = buffer;
            source.connect(this.context!.destination);
            source.onended = () => {
              if (this.output === source) this.output = undefined;
              source.disconnect();
              resolve();
            };
            this.output = source;
            if (!this.firstAudio) {
              this.firstAudio = true;
              this.patch({
                metrics: {
                  ...this.snapshot.metrics,
                  first_audio_scheduled_ms: Math.round(performance.now() - this.speechEnd),
                  audio_output_latency_ms: Math.round(1000 * (this.context!.baseLatency || 0)),
                },
              });
            }
            source.start();
          });
        });
        // Next sentence may synthesize while this sentence plays.
      })
      .catch(error => {
        if (this.current(epoch) && !signal.aborted) {
          this.problem(error);
          throw error;
        }
      });
  }
  private async complete(epoch: number) {
    await this.synthTail;
    await this.outputTail;
    if (!this.current(epoch)) return;
    this.pending = undefined;
    this.persist();
    this.busy = false;
    this.state('listening', 'Microphone active · your turn');
  }
  private stopOutput() {
    this.synthAbort.abort();
    this.synthAbort = new AbortController();
    try {
      this.output?.stop();
    } catch {
      /* already ended */
    }
    this.output = undefined;
    this.outputTail = Promise.resolve();
    this.synthTail = Promise.resolve();
  }
  interrupt() {
    if (this.closed) return;
    this.interruptedAt = performance.now();
    ++this.epoch;
    this.turn.abort();
    this.turn = new AbortController();
    this.stopOutput();
    if (this.pending?.response)
      void this.api.cancel(this.pending.conversation, this.pending.response).catch(() => {});
    // If acceptance is uncertain retain the key and block another submission until reconciliation.
    if (this.pending && !this.pending.response) {
      this.state('disconnected', 'Submission uncertain. Recovering before another turn.');
      this.busy = true;
      this.reconcilingCancellation = true;
      void this.cancelUncertain(this.pending, this.epoch).catch(error => this.problem(error));
      return;
    }
    this.pending = undefined;
    this.persist();
    this.busy = false;
    this.patch({
      metrics: {
        ...this.snapshot.metrics,
        interrupt_stop_ms: Math.round(performance.now() - this.interruptedAt),
      },
    });
    this.state('interrupted', 'Interrupted · hearing you');
  }
  private async cancelUncertain(attempt: Pending, epoch: number) {
    while (this.current(epoch)) {
      try {
        const accepted = await this.api.submit(
          attempt.conversation,
          { content: attempt.content },
          attempt.key
        );
        if (!this.current(epoch)) return;
        attempt.response = accepted.response_id;
        this.persist();
        await this.api.cancel(attempt.conversation, accepted.response_id);
        if (!this.current(epoch)) return;
        this.pending = undefined;
        this.persist();
        this.busy = false;
        this.reconcilingCancellation = false;
        this.state('listening', 'Interrupted request reconciled · speak again');
        return;
      } catch (error) {
        const status = (error as { status?: number }).status;
        if (status && status < 500 && status !== 429) throw error;
        await sleep(2000, this.turn.signal);
      }
    }
  }
  mute() {
    const muted = !this.snapshot.muted;
    this.media?.getAudioTracks().forEach(track => {
      track.enabled = !muted;
    });
    this.detector.reset();
    this.frames = [];
    this.preroll = [];
    this.patch({ muted });
    this.state(
      this.output ? 'speaking' : this.busy ? 'thinking' : 'listening',
      muted ? 'Microphone muted' : 'Microphone active'
    );
  }
  selectVoice(voice: string) {
    if (['af_bella', 'am_adam'].includes(voice)) this.voice = voice;
  }
  async sink(id: string) {
    if (this.context?.setSinkId) await this.context.setSinkId(id);
  }
  get supportsSink() {
    return !!this.context?.setSinkId;
  }
  private problem(error: unknown) {
    if (this.closed) return;
    this.stopOutput();
    this.busy = false;
    this.state(
      'disconnected',
      error instanceof Error ? error.message : 'Voice failed; text chat remains available'
    );
  }
  end() {
    if (this.closed) return;
    this.closed = true;
    ++this.epoch;
    this.life.abort();
    this.turn.abort();
    this.stopOutput();
    // Closing media must not cancel durable history: refresh resumes this request.
    this.media?.getTracks().forEach(track => {
      track.onended = null;
      track.stop();
    });
    if (this.worklet) {
      this.worklet.port.onmessage = null;
      this.worklet.disconnect();
    }
    this.source?.disconnect();
    this.zero?.disconnect();
    void this.context?.close();
    window.removeEventListener('online', this.online);
    this.snapshot = {
      ...this.snapshot,
      state: 'ended',
      level: 0,
      message: 'Call ended · microphone off',
    };
    this.notify(this.snapshot);
  }
}

export function VoiceCall({
  api,
  conversationId,
  disabled,
  active,
}: {
  api: WorkspaceApi;
  conversationId: string | null;
  disabled: boolean;
  active: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const session = useRef<CallSession | undefined>(undefined);
  const trigger = useRef<HTMLButtonElement>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const cache = useQueryClient();
  useEffect(() => () => session.current?.end(), []);
  useEffect(() => {
    if (!active) {
      session.current?.end();
      setOpen(false);
    }
  }, [active]);
  useEffect(() => {
    if (!open) return;
    dialog.current?.focus();
    const end = () => session.current?.end();
    window.addEventListener('pagehide', end);
    return () => window.removeEventListener('pagehide', end);
  }, [open]);
  const end = () => {
    session.current?.end();
    setOpen(false);
    trigger.current?.focus();
  };
  const start = () => {
    if (session.current) session.current.end();
    const call = new CallSession(
      api,
      conversationId,
      next => {
        setSnapshot({ ...next });
      },
      id => {
        void cache.invalidateQueries({ queryKey: conversationListKey });
        void cache.invalidateQueries({ queryKey: detailKey(id) });
      }
    );
    session.current = call;
    setOpen(true);
    void call
      .start()
      .then(async () =>
        setDevices(
          (await navigator.mediaDevices.enumerateDevices()).filter(
            device => device.kind === 'audiooutput'
          )
        )
      )
      .catch(() => {});
  };
  return (
    <>
      <button
        ref={trigger}
        type="button"
        className="th-composer-attach th-voice-trigger"
        disabled={disabled}
        onClick={start}
        aria-label="Start voice call"
        title="Continuous local voice conversation"
      >
        ◉ Voice
      </button>
      {open &&
        createPortal(
          <div className="th-voice-shade">
            <div
              ref={dialog}
              tabIndex={-1}
              role="dialog"
              aria-modal="true"
              aria-label="Thozha voice call"
              className="th-voice-call"
              data-state={snapshot?.state}
              onKeyDown={event => {
                if (event.key === 'Escape') {
                  event.preventDefault();
                  end();
                }
                if (event.altKey && event.key.toLowerCase() === 'm') {
                  event.preventDefault();
                  session.current?.mute();
                }
                if (event.altKey && event.key.toLowerCase() === 'i') {
                  event.preventDefault();
                  session.current?.interrupt();
                }
                if (event.key === 'Tab') {
                  const nodes = dialog.current?.querySelectorAll<HTMLElement>(
                    'button:not(:disabled), select'
                  );
                  if (nodes?.length) {
                    const first = nodes[0],
                      last = nodes[nodes.length - 1];
                    if (
                      event.shiftKey &&
                      (document.activeElement === first ||
                        document.activeElement === dialog.current)
                    ) {
                      event.preventDefault();
                      last.focus();
                    } else if (!event.shiftKey && document.activeElement === last) {
                      event.preventDefault();
                      first.focus();
                    }
                  }
                }
              }}
            >
              <div className="th-voice-heading">
                <span>THOZHA · LOCAL VOICE</span>
                <button onClick={end} aria-label="End voice call">
                  End call ×
                </button>
              </div>
              <div
                className="th-voice-orb"
                aria-hidden="true"
                style={{ transform: `scale(${1 + (snapshot?.level || 0) * 0.16})` }}
              />
              <h2>{snapshot?.state || 'connecting'}</h2>
              <p role="status">{snapshot?.message}</p>
              <p className="th-voice-privacy">
                {snapshot?.state === 'ended'
                  ? '● Microphone off'
                  : snapshot?.muted
                    ? '● Microphone muted'
                    : '● Microphone active during this call'}{' '}
                · local ASR and neural speech · no cloud audio
              </p>
              <div
                className="th-voice-transcript"
                role="log"
                aria-label="Live voice transcript"
                aria-live="polite"
              >
                {snapshot?.user && (
                  <p>
                    <b>You</b>
                    {snapshot.user}
                  </p>
                )}
                {snapshot?.assistant && (
                  <p>
                    <b>Thozha</b>
                    {snapshot.assistant}
                  </p>
                )}
              </div>
              <div className="th-voice-controls">
                <button
                  onClick={() => session.current?.mute()}
                  aria-pressed={snapshot?.muted}
                  disabled={snapshot?.state === 'ended'}
                >
                  {snapshot?.muted ? 'Unmute' : 'Mute'} · ⌥M
                </button>
                <button
                  onClick={() => session.current?.interrupt()}
                  disabled={snapshot?.state === 'ended'}
                >
                  Interrupt · ⌥I
                </button>
                <label>
                  Voice
                  <select
                    aria-label="Neural voice"
                    onChange={event => session.current?.selectVoice(event.target.value)}
                  >
                    <option value="af_bella">Bella · English</option>
                    <option value="am_adam">Adam · English</option>
                  </select>
                </label>
                {session.current?.supportsSink && (
                  <label>
                    Speaker
                    <select
                      aria-label="Audio output"
                      onChange={event => {
                        void session.current?.sink(event.target.value).catch(() =>
                          setSnapshot(
                            previous =>
                              previous && {
                                ...previous,
                                message:
                                  'Output selection failed. Default speaker remains available.',
                              }
                          )
                        );
                      }}
                    >
                      <option value="">System default</option>
                      {devices
                        .filter(d => d.deviceId !== 'default')
                        .map(device => (
                          <option key={device.deviceId} value={device.deviceId}>
                            {device.label || 'Speaker'}
                          </option>
                        ))}
                    </select>
                  </label>
                )}
              </div>
              <details>
                <summary>Timing · milliseconds · scheduled audio, not acoustic proof</summary>
                <pre>{JSON.stringify(snapshot?.metrics || {}, null, 2)}</pre>
              </details>
              <small>
                Use headphones if speaker echo causes interruptions. Esc ends the call. Tools and
                sensitive actions still require the existing approvals.
              </small>
            </div>
          </div>,
          document.body
        )}
    </>
  );
}
