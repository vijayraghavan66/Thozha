import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useQueryClient } from '@tanstack/react-query';
import type { WorkspaceApi } from '../api/client';
import { conversationListKey, detailKey } from '../chat/updates';
import { splitSpeech, wav16, type CallState } from './turns';
import type { MicVAD } from '@ricky0123/vad-web';
import { BargeProbe, SpeechGate, normalizeSpeech } from './acoustics';
import { orderedInference } from './inference';
import { PlaybackReference, ResumablePlayback } from './playback';
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
  private output?: { stop(): void };
  private playback?: ResumablePlayback;
  private reference?: PlaybackReference;
  private semanticReady = false;
  private endpointBusy = false;
  private endpointRetryAt = 0;
  private speechRevision = 0;
  private candidate = false;
  private candidateText = '';
  private playbackText = '';
  private confirmingInterruption = false;
  private confirmation = new AbortController();
  private detector = new SpeechGate();
  private vad?: MicVAD;
  private inference?: ReturnType<typeof orderedInference>;
  private captureMetrics: Record<string, number> = {};
  private captureStarted = 0;
  private capturedSamples = 0;
  private segmentMetrics: Record<string, number> = {};
  private lastMeterAt = 0;
  private barging = false;
  private verifyingBarge = false;
  private lastSpeechAt = 0;
  private playbackUntil = 0;
  private confirmingAt = 0;
  private playbackGain?: GainNode;
  private probe = new BargeProbe();
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
      this.playbackGain = this.context.createGain();
      this.playbackGain.connect(this.context.destination);
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
      this.semanticReady = !!health.semantic_turn;
      this.detector.endpointMs = this.semanticReady ? 320 : 800;
      this.reference = new PlaybackReference(this.context, this.playbackGain!, ready => {
        this.captureMetrics.playback_reference_rtc = Number(ready);
      });
      await this.reference.start();
      if (this.closed) return;
      const { MicVAD } = await import('@ricky0123/vad-web');
      const vad = await MicVAD.new({
        model: 'v5',
        baseAssetPath: '/voice-assets/',
        onnxWASMBasePath: '/voice-assets/',
        audioContext: this.context,
        startOnLoad: false,
        processorType: 'AudioWorklet',
        getStream: async () => this.media!,
        pauseStream: async () => {},
        resumeStream: async () => this.media!,
        ortConfig: ort => {
          ort.env.wasm.numThreads = 1;
          ort.env.logLevel = 'error';
        },
        onFrameProcessed: (probabilities, frame) => this.frame(frame, probabilities.isSpeech),
        submitUserSpeechOnPause: false,
      });
      // The library's async MessagePort callback does not serialize recurrent
      // Silero state. Preserve sample order and surface overload, never drop audio.
      const process = vad.processFrame.bind(vad);
      this.inference = orderedInference(
        process,
        (depth, ms, wait) => {
          this.captureMetrics.vad_queue_peak = Math.max(
            this.captureMetrics.vad_queue_peak || 0,
            depth
          );
          this.captureMetrics.vad_inference_ms = Math.round(ms);
          this.captureMetrics.vad_queue_wait_ms = Math.round(wait);
        },
        () => {
          this.end();
          this.notify({
            ...this.snapshot,
            message: 'Audio processing overloaded. Microphone stopped; close and retry.',
          });
        }
      );
      vad.processFrame = frame => this.inference!.feed(frame);
      this.vad = vad;
      this.captureStarted = performance.now();
      this.captureMetrics = {
        ...this.captureMetrics, // Preserve the playback-reference startup result.
        capture_started_ms: Math.round(this.captureStarted),
        context_sample_rate: this.context.sampleRate,
        track_sample_rate: this.media.getAudioTracks()[0]?.getSettings().sampleRate || 0,
        asr_sample_rate: 16000,
        // Browser/driver drops are not exposed by MicVAD; never assert zero.
        dropped_frames: -1,
        echo_cancellation: Number(
          this.media.getAudioTracks()[0]?.getSettings().echoCancellation === true
        ),
        noise_suppression: Number(
          this.media.getAudioTracks()[0]?.getSettings().noiseSuppression === true
        ),
        automatic_gain_control: Number(
          this.media.getAudioTracks()[0]?.getSettings().autoGainControl === true
        ),
      };
      await vad.start();
      if (this.closed) {
        await vad.destroy();
        return;
      }
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
      const message =
        error instanceof Error ? `${error.name}: ${error.message}` : 'Microphone unavailable';
      this.end();
      this.snapshot = { ...this.snapshot, state: 'ended', message };
      this.notify(this.snapshot);
    }
  }
  private frame(data: Float32Array, probability: number) {
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
    const now = performance.now();
    this.capturedSamples += data.length;
    this.captureMetrics.processed_audio_ms = Math.round(this.capturedSamples / 16);
    this.captureMetrics.capture_elapsed_ms = Math.round(now - this.captureStarted);
    if (level > 0.002 && !this.captureMetrics.first_non_silent_ms)
      this.captureMetrics.first_non_silent_ms = Math.round(now - this.captureStarted);
    // Do not render React at the 31 Hz inference callback rate.
    if (now - this.lastMeterAt >= 100) {
      this.lastMeterAt = now;
      this.patch({ level: Math.min(1, level * 12) });
    }
    const playback = !!this.output || performance.now() < this.playbackUntil;
    if (this.verifyingBarge) {
      this.frames.push(data);
      const decision = this.probe.feed(probability, level, data.length / 16);
      if (decision) {
        this.verifyingBarge = false;
        this.barging = false;
        if (decision === 'accept') {
          this.candidate = true;
          this.candidateText = this.playbackText;
          this.playback?.pause();
          this.setPlaybackLevel(1);
          this.detector.resumeConfirmedSpeech();
          this.lastSpeechAt = now;
          this.patch({
            metrics: {
              ...this.snapshot.metrics,
              barge_pause_ms: Math.round(now - this.confirmingAt),
              barge_pause_wall_ms: Date.now(),
            },
          });
          this.state('interrupted', 'Listening · response paused, not discarded');
        } else {
          this.setPlaybackLevel(1);
          this.detector.reset();
          this.frames = [];
          this.preroll = [];
        }
      }
      return;
    }
    if (
      this.reconcilingCancellation ||
      (this.busy && !playback && !this.barging && !this.candidate)
    )
      return;
    if (probability >= 0.35) {
      this.lastSpeechAt = performance.now();
      ++this.speechRevision;
    }
    const wasSpeaking = this.detector.speaking;
    const event = this.detector.feed(
      probability,
      level,
      data.length / 16,
      playback && !this.candidate
    );
    this.preroll.push(data);
    while (this.preroll.length > 25) this.preroll.shift();
    if (event === 'discard') {
      this.frames = [];
      this.preroll = [];
      return;
    }
    if (event === 'start') {
      this.captureMetrics.vad_start_ms = Math.round(performance.now() - this.captureStarted);
      this.barging = playback;
      this.confirmingAt = performance.now();
      this.frames = [...this.preroll];
      if (!playback) this.state('listening', 'Hearing you · take your time');
    } else if (wasSpeaking) this.frames.push(data);
    if (event === 'start' && playback && !this.candidate) {
      // Attenuate the echo source before classifying residual near-end speech.
      // This is reversible; no request cancellation or history mutation yet.
      this.verifyingBarge = true;
      this.probe.reset();
      this.patch({
        metrics: {
          ...this.snapshot.metrics,
          barge_probes: (this.snapshot.metrics.barge_probes || 0) + 1,
          barge_probe_wall_ms: Date.now(),
        },
      });
      this.setPlaybackLevel(0.03);
      return;
    }
    if (
      event === 'end' &&
      this.frames.length &&
      !this.confirmingInterruption &&
      !(this.pending && !this.pending.response)
    ) {
      this.detector.resumeConfirmedSpeech();
      if (now >= this.endpointRetryAt && !this.endpointBusy) void this.checkEndpoint();
    }
  }
  private samples() {
    const samples = new Float32Array(this.frames.reduce((n, frame) => n + frame.length, 0));
    let offset = 0;
    for (const frame of this.frames) {
      samples.set(frame, offset);
      offset += frame.length;
    }
    return samples;
  }
  private async checkEndpoint() {
    this.endpointBusy = true;
    const revision = this.speechRevision;
    const epoch = this.epoch;
    try {
      const silence = performance.now() - this.lastSpeechAt;
      const segmentSamples = this.frames.reduce((count, frame) => count + frame.length, 0);
      // Repeated semantic deferrals must not exceed the bounded ASR contract.
      if (this.semanticReady && silence < 3000 && segmentSamples < 29 * 16000) {
        const row = await this.speech('endpoint', {
          audio: wav16(this.samples().slice(-128000), 16000),
        });
        if (
          this.closed ||
          this.snapshot.muted ||
          epoch !== this.epoch ||
          revision !== this.speechRevision
        )
          return;
        this.patch({
          metrics: {
            ...this.snapshot.metrics,
            semantic_ms: row.semantic_ms,
            turn_complete_probability: row.probability,
          },
        });
        if (!row.complete) {
          this.endpointRetryAt = performance.now() + 400;
          return;
        }
      }
      if (
        this.closed ||
        this.snapshot.muted ||
        epoch !== this.epoch ||
        revision !== this.speechRevision
      )
        return;
      const samples = this.samples();
      this.frames = [];
      this.preroll = [];
      this.detector.reset();
      this.speechEnd = this.lastSpeechAt;
      this.segmentMetrics = {
        ...this.captureMetrics,
        endpoint_wait_ms: Math.round(performance.now() - this.speechEnd),
        segment_samples: samples.length,
        segment_duration_ms: samples.length / 16,
      };
      if (this.candidate) void this.confirmInterruption(wav16(samples, 16000));
      else void this.respond(wav16(samples, 16000));
    } catch {
      // Failed semantics lengthens endpointing; never silently clip a thinking pause.
      this.semanticReady = false;
      this.detector.endpointMs = 1200;
    } finally {
      this.endpointBusy = false;
    }
  }
  private async confirmInterruption(audio: string) {
    if (this.confirmingInterruption) return;
    this.confirmingInterruption = true;
    const epoch = this.epoch;
    const started = performance.now();
    try {
      const row = await this.speech('transcribe', { audio }, this.confirmation.signal);
      if (!this.current(epoch) || !this.candidate) return;
      const text = String(row.text || '').trim();
      const normalize = (s: string) =>
        s
          .toLowerCase()
          .replace(/[^a-z0-9' ]/g, ' ')
          .replace(/\s+/g, ' ')
          .trim();
      const words = normalize(text);
      const echo = words.length > 0 && normalize(this.candidateText).includes(words);
      if (!words || echo) {
        this.candidate = false;
        this.playback?.play();
        this.patch({
          metrics: {
            ...this.snapshot.metrics,
            false_interruptions_resumed:
              (this.snapshot.metrics.false_interruptions_resumed || 0) + 1,
          },
        });
        this.state('speaking', 'Continuing · no new request');
        return;
      }
      this.candidate = false;
      this.interrupt(true);
      this.patch({
        metrics: {
          ...this.segmentMetrics,
          ...row.metrics,
          transcript_ms: Math.round(performance.now() - this.speechEnd),
          interruption_asr_ms: Math.round(performance.now() - started),
          barge_confirmed: (this.snapshot.metrics.barge_confirmed || 0) + 1,
        },
      });
      void this.respond('', text);
    } catch {
      if (this.current(epoch) && this.candidate) {
        this.candidate = false;
        this.playback?.play();
        this.state('speaking', 'Recognition unavailable · continuing response');
      }
    } finally {
      this.confirmingInterruption = false;
    }
  }

  private setPlaybackLevel(value: number) {
    if (this.playbackGain && this.context) {
      this.playbackGain.gain.cancelScheduledValues(this.context.currentTime);
      this.playbackGain.gain.setTargetAtTime(value, this.context.currentTime, 0.015);
    }
  }
  private persist() {
    if (this.pending) sessionStorage.setItem(storage, JSON.stringify(this.pending));
    else sessionStorage.removeItem(storage);
  }
  private current(epoch: number) {
    return !this.closed && this.epoch === epoch;
  }
  private async respond(audio: string, recognized?: string) {
    const epoch = ++this.epoch;
    this.turn.abort();
    this.turn = new AbortController();
    this.busy = true;
    this.firstAudio = false;
    const uploadStarted = performance.now();
    this.patch({ metrics: { ...this.segmentMetrics }, user: '', assistant: '' });
    this.state('thinking', 'Transcribing locally…');
    try {
      const result =
        recognized === undefined
          ? await this.speech('transcribe', { audio }, this.turn.signal)
          : { text: recognized, metrics: {} };
      if (!this.current(epoch)) return;
      const text = String(result.text || '').trim();
      if (!text) {
        this.busy = false;
        this.state('listening', 'No speech recognized · try again');
        return;
      }
      this.patch({
        user: text,
        metrics: {
          ...this.snapshot.metrics,
          ...result.metrics,
          upload_to_transcript_ms: Math.round(performance.now() - uploadStarted),
          transcript_ms: Math.round(performance.now() - this.speechEnd),
        },
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
  private async recover(attempt: Pending, epoch: number) {
    let delay = 250;
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
          for (const sentence of splitSpeech(row.content, true, 120).sentences)
            this.enqueue(sentence, epoch);
          await this.complete(epoch);
          return;
        }
        if (row && !['running', 'queued'].includes(row.status))
          throw new Error(`Response ${row.status}`);
        this.state('thinking', 'Reconciling saved answer · no duplicate submission');
        delay = 250;
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
        const synthesisStarted = performance.now();
        const row = await this.speech(
          'synthesize',
          { text: normalizeSpeech(sentence).slice(0, 400), voice },
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
          // A suspected interruption may arrive on a chunk boundary. Never start
          // another voice while confirmation is retaining the current position.
          while ((this.candidate || this.verifyingBarge) && this.current(epoch) && !signal.aborted)
            await sleep(25, signal);
          if (!this.current(epoch) || signal.aborted || !this.context) return;
          this.state('speaking', 'Thozha speaking · you can interrupt');
          const player = new ResumablePlayback(
            this.context!,
            buffer,
            this.playbackGain || this.context!.destination
          );
          this.playback = player;
          this.playbackText = sentence;
          this.output = player;
          if (!this.firstAudio) {
            this.firstAudio = true;
            this.patch({
              metrics: {
                ...this.snapshot.metrics,
                first_audio_scheduled_ms: Math.round(performance.now() - this.speechEnd),
                first_audio_wall_ms: Date.now(),
                first_tts_pipeline_ms: Math.round(performance.now() - synthesisStarted),
                first_tts_generation_ms: Number(row.synthesis_ms || 0),
                supervisor_wait_ms:
                  (this.snapshot.metrics.response_ms || 0) -
                  (this.snapshot.metrics.acceptance_ms || 0),
                audio_output_latency_ms: Math.round(1000 * (this.context!.baseLatency || 0)),
              },
            });
          }
          player.play();
          await player.done;
          if (this.playback === player) {
            this.playback = undefined;
            this.output = undefined;
            this.playbackUntil = performance.now() + 350;
          }
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
    // A chunk can finish while residual speech is being checked. Retain the
    // response and playback ownership until confirmation either resumes or
    // supersedes it; otherwise a late confirmation can submit a duplicate turn.
    while ((this.candidate || this.verifyingBarge) && this.current(epoch))
      await sleep(25, this.turn.signal);
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
    this.playback = undefined;
    this.playbackUntil = 0;
    this.probe.reset();
    this.setPlaybackLevel(1);
    this.outputTail = Promise.resolve();
    this.synthTail = Promise.resolve();
  }
  interrupt(preserveCapture = false) {
    if (this.closed) return;
    this.confirmation.abort();
    this.confirmation = new AbortController();
    this.candidate = false;
    if (!preserveCapture) {
      this.detector.reset();
      this.frames = [];
      this.preroll = [];
    }
    ++this.speechRevision; // Invalidate semantic work from before interruption.
    this.interruptedAt = performance.now();
    this.verifyingBarge = this.barging = false;
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
    ++this.speechRevision; // Invalidate an in-flight semantic endpoint.
    this.detector.reset();
    this.frames = [];
    this.preroll = [];
    this.barging = this.verifyingBarge = false;
    this.probe.reset();
    this.confirmation.abort();
    this.confirmation = new AbortController();
    this.candidate = false;
    this.playback?.play();
    this.setPlaybackLevel(1);
    this.patch({ muted });
    this.state(
      this.output ? 'speaking' : this.busy ? 'thinking' : 'listening',
      muted ? 'Microphone muted' : 'Microphone active'
    );
  }
  selectVoice(voice: string) {
    if (['af_bella', 'am_adam', 'qwen_ryan'].includes(voice)) this.voice = voice;
  }
  async sink(id: string) {
    if (this.reference && this.captureMetrics.playback_reference_rtc) await this.reference.sink(id);
    else if (this.context?.setSinkId) await this.context.setSinkId(id);
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
    this.confirmation.abort();
    this.reference?.close();
    this.turn.abort();
    this.inference?.stop();
    this.frames = [];
    this.preroll = [];
    this.detector.reset();
    void this.vad?.destroy().catch(() => {});
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
    this.playbackGain?.disconnect();
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
                    <option value="qwen_ryan">Ryan · expressive Qwen · slower first use</option>
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
