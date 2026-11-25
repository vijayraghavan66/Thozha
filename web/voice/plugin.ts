import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createInterface } from 'node:readline';
import { timingSafeEqual, randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import type { IncomingMessage } from 'node:http';
import type { Plugin } from 'vite';

export const voiceOrigin = 'http://127.0.0.1:5176';
export const webOrigin = 'http://127.0.0.1:5174';
const routes = new Set([
  '/v1/web/voice/health',
  '/v1/web/voice/transcribe',
  '/v1/web/voice/synthesize',
]);
export function validVoiceRequest(req: Pick<IncomingMessage, 'headers' | 'method'>) {
  return (
    req.headers.host === new URL(voiceOrigin).host &&
    req.headers.origin === voiceOrigin &&
    (!req.headers['sec-fetch-site'] || req.headers['sec-fetch-site'] === 'same-origin') &&
    req.method === 'POST' &&
    req.headers['content-type'] === 'application/json'
  );
}
export function equalSecret(a: string, b: string) {
  return (
    !!a &&
    Buffer.byteLength(a) === Buffer.byteLength(b) &&
    timingSafeEqual(Buffer.from(a), Buffer.from(b))
  );
}
export function localVoice(): Plugin {
  let child: ChildProcessWithoutNullStreams | undefined;
  let ready = false;
  const pending = new Map<
    string,
    { resolve: (row: Record<string, unknown>) => void; reject: (error: Error) => void }
  >();
  function stop() {
    ready = false;
    child?.kill('SIGTERM');
    child = undefined;
    for (const entry of pending.values()) entry.reject(new Error('Local speech worker stopped'));
    pending.clear();
  }
  function start() {
    if (child) return;
    const python =
      process.env.THOZHA_VOICE_PYTHON ||
      `${homedir()}/Documents/Jarvis-Lab/agent-evaluation/thozha-voice-mac-venv/bin/python`;
    child = spawn(python, ['-B', `${process.cwd()}/voice/worker.py`], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: {
        ...process.env,
        HF_HUB_OFFLINE: '1',
        TRANSFORMERS_OFFLINE: '1',
        TOKENIZERS_PARALLELISM: 'false',
      },
    });
    // Never relay model/library diagnostics or request data to HTTP clients.
    child.stderr.on('data', () => {});
    createInterface({ input: child.stdout }).on('line', line => {
      try {
        const row = JSON.parse(line);
        if (row.id === 'ready') {
          ready = true;
          return;
        }
        const item = pending.get(row.id);
        if (item && row.done) {
          pending.delete(row.id);
          item.resolve(row);
        }
      } catch {
        /* malformed worker output cannot enter the browser */
      }
    });
    child.on('error', stop);
    child.on('exit', () => {
      ready = false;
      child = undefined;
      for (const item of pending.values()) item.reject(new Error('Speech worker exited'));
      pending.clear();
    });
  }
  return {
    name: 'thozha-private-local-voice',
    configureServer(server) {
      server.httpServer?.once('close', stop);
      server.middlewares.use(async (req, res, next) => {
        const path = req.url?.split('?')[0] || '';
        if (path.startsWith('/v1/') && !path.startsWith('/v1/web/')) {
          res.statusCode = 404;
          res.end();
          return;
        }
        if (!path.startsWith('/v1/web/voice/')) {
          // Explicit development-origin bridge to the unchanged authenticated Web app.
          // Reject foreign Origins before translating to its configured canonical origin.
          if (path.startsWith('/v1/web/')) {
            if (
              req.headers.host !== new URL(voiceOrigin).host ||
              (req.headers.origin && req.headers.origin !== voiceOrigin)
            ) {
              res.statusCode = 403;
              res.end();
              return;
            }
            if (req.headers.origin) req.headers.origin = webOrigin;
          }
          return next();
        }
        const send = (status: number, row: unknown) => {
          if (res.writableEnded || res.destroyed) return;
          res.statusCode = status;
          res.setHeader('Content-Type', 'application/json');
          res.setHeader('Cache-Control', 'no-store');
          res.end(JSON.stringify(row));
        };
        if (!routes.has(path)) return send(404, { error: 'Unknown voice route' });
        if (!validVoiceRequest(req))
          return send(403, { error: 'Same-origin JSON request required' });
        try {
          const auth = await fetch(`${webOrigin}/v1/web/session`, {
            headers: { cookie: req.headers.cookie || '', host: new URL(webOrigin).host },
            redirect: 'error',
            signal: AbortSignal.timeout(5000),
          });
          if (!auth.ok)
            return send(auth.status === 401 ? 401 : 503, {
              error: 'Existing Web session required',
            });
          const session = await auth.json();
          if (!equalSecret(String(req.headers['x-thozha-csrf'] || ''), session.csrf_token))
            return send(403, { error: 'CSRF rejected' });
          const chunks: Buffer[] = [];
          let bytes = 0;
          for await (const chunk of req) {
            bytes += chunk.length;
            if (bytes > 1450000) return send(413, { error: 'Voice request too large' });
            chunks.push(Buffer.from(chunk));
          }
          const body = JSON.parse(Buffer.concat(chunks).toString());
          start();
          if (path.endsWith('/health'))
            return send(200, {
              ready,
              engine: 'Kokoro-82M MLX',
              stt: 'local faster-whisper',
              partial_transcription: false,
            });
          if (!ready)
            return send(503, { error: 'Local neural voice is warming up. Retry shortly.' });
          if (pending.size >= 3) return send(429, { error: 'Voice worker busy' });
          const action = path.endsWith('/transcribe') ? 'stt' : 'tts';
          if (action === 'stt' && (typeof body.audio !== 'string' || body.audio.length > 1400000))
            return send(422, { error: 'Invalid audio' });
          if (
            action === 'tts' &&
            (typeof body.text !== 'string' ||
              !body.text.trim() ||
              body.text.length > 400 ||
              !['af_bella', 'am_adam'].includes(body.voice))
          )
            return send(422, { error: 'Invalid synthesis request' });
          const id = randomUUID();
          const cancel = () => {
            if (!pending.has(id)) return;
            child?.stdin.write(JSON.stringify({ id, action: 'cancel' }) + '\n');
            pending.get(id)?.reject(new Error('Voice request cancelled'));
            pending.delete(id);
          };
          res.once('close', cancel);
          const timer = setTimeout(cancel, 60000);
          try {
            const result = await new Promise<Record<string, unknown>>((resolve, reject) => {
              pending.set(id, { resolve, reject });
              child!.stdin.write(
                JSON.stringify({
                  id,
                  action,
                  ...(action === 'stt'
                    ? { audio: body.audio }
                    : { text: body.text, voice: body.voice }),
                }) + '\n'
              );
            });
            delete result.id;
            send(result.error ? 503 : 200, result);
          } finally {
            clearTimeout(timer);
            res.off('close', cancel);
          }
        } catch {
          send(503, { error: 'Local voice unavailable. Text chat is unchanged.' });
        }
      });
    },
  };
}
