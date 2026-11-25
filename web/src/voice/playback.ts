/** Reversible interruption. Retains the exact unplayed sample position, not text estimates. */
export class ResumablePlayback {
  private source?: AudioBufferSourceNode;
  private offset = 0;
  private started = 0;
  private finished = false;
  private resolve!: () => void;
  readonly done = new Promise<void>(resolve => {
    this.resolve = resolve;
  });
  constructor(
    private context: AudioContext,
    private buffer: AudioBuffer,
    private target: AudioNode
  ) {}
  play() {
    if (this.finished || this.source) return;
    const source = this.context.createBufferSource();
    source.buffer = this.buffer;
    source.connect(this.target);
    this.source = source;
    this.started = this.context.currentTime;
    source.onended = () => {
      source.disconnect();
      if (this.source !== source) return;
      this.source = undefined;
      this.finished = true;
      this.resolve();
    };
    source.start(0, this.offset);
  }
  pause() {
    const source = this.source;
    if (!source || this.finished) return;
    this.offset = Math.min(
      this.buffer.duration,
      this.offset + this.context.currentTime - this.started
    );
    this.source = undefined;
    source.stop();
    source.disconnect();
  }
  stop() {
    this.pause();
    this.finished = true;
    this.resolve();
  }
}

/** Standard local WebRTC media loop: routes only synthesized output through the
 * browser RTC render path, providing an AEC playback reference. No mic track,
 * signaling server, STUN, TURN, or remote peer is used. Not acoustic separation.
 * Implemented independently; no GPL Sonia source is copied.
 */
export class PlaybackReference {
  private sender = new RTCPeerConnection({ iceServers: [] });
  private receiver = new RTCPeerConnection({ iceServers: [] });
  private audio = new Audio();
  private destination: MediaStreamAudioDestinationNode;
  private closed = false;
  private timer?: ReturnType<typeof setTimeout>;
  constructor(
    private context: AudioContext,
    private gain: GainNode,
    private status: (ready: boolean) => void
  ) {
    this.destination = context.createMediaStreamDestination();
  }
  async start() {
    const { sender, receiver, audio } = this;
    try {
      sender.onicecandidate = event => {
        if (event.candidate) void receiver.addIceCandidate(event.candidate).catch(() => {});
      };
      receiver.onicecandidate = event => {
        if (event.candidate) void sender.addIceCandidate(event.candidate).catch(() => {});
      };
      const track = new Promise<void>(resolve => {
        receiver.ontrack = event => {
          audio.srcObject = event.streams[0];
          resolve();
        };
      });
      this.destination.stream
        .getTracks()
        .forEach(item => sender.addTrack(item, this.destination.stream));
      const offer = await sender.createOffer();
      await sender.setLocalDescription(offer);
      await receiver.setRemoteDescription(offer);
      const answer = await receiver.createAnswer();
      await receiver.setLocalDescription(answer);
      await sender.setRemoteDescription(answer);
      await Promise.race([
        Promise.all([
          track,
          new Promise<void>((resolve, reject) => {
            sender.onconnectionstatechange = () => {
              if (sender.connectionState === 'connected') resolve();
              if (sender.connectionState === 'failed') reject(new Error('Local RTC unavailable'));
            };
            if (sender.connectionState === 'connected') resolve();
          }),
        ]),
        new Promise<never>((_, reject) => {
          this.timer = setTimeout(() => reject(new Error('Local RTC timeout')), 2500);
        }),
      ]);
      await audio.play();
      if (this.closed) return;
      this.gain.disconnect();
      this.gain.connect(this.destination);
      this.status(true);
      sender.onconnectionstatechange = () => {
        if (['failed', 'disconnected', 'closed'].includes(sender.connectionState)) this.fallback();
      };
    } catch {
      this.fallback();
    } finally {
      clearTimeout(this.timer);
    }
  }
  private fallback() {
    if (this.closed) return;
    this.gain.disconnect();
    this.gain.connect(this.context.destination);
    this.status(false);
    this.closePeers();
  }
  async sink(id: string) {
    await this.audio.setSinkId(id);
  }
  private closePeers() {
    this.sender.onconnectionstatechange = null;
    this.sender.onicecandidate = this.receiver.onicecandidate = null;
    this.receiver.ontrack = null;
    this.sender.close();
    this.receiver.close();
    this.audio.pause();
    this.audio.srcObject = null;
    this.destination.stream.getTracks().forEach(track => track.stop());
  }
  close() {
    this.closed = true;
    // Do not clear a startup timeout: start() may still be waiting for the
    // connection promise, which must settle even when End is pressed early.
    this.closePeers();
  }
}
