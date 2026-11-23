#!/usr/bin/env python3
"""Loopback-only Thozha Live bridge for USB reverse development sessions.

The Android companion authenticates with its already-paired device token. This
process reads only the paired-device token table, accepts bounded PCM turns,
transcribes with MLX Whisper, asks the loopback OpenJarvis chat engine for
text-only responses (no tools are exposed), then streams MLX Qwen3-TTS PCM.
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
import ipaddress
import json
import logging
import os
from pathlib import Path
import struct
import threading
import time
from typing import Any
import sys

import mlx.core as mx
import mlx_whisper
import numpy as np
from mlx_audio.tts.utils import load_model
from websockets.asyncio.client import connect as websocket_connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

_HOST_PACKAGE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "jarvis_link_host")
if _HOST_PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _HOST_PACKAGE_DIR)
from voice_protocol import AudioTurnBuffer, PcmFrame, VoiceProtocolError, decode_frame, encode_frame
from voice_security import authenticate_paired_device, valid_loopback_peer

LOG = logging.getLogger("thozha.live")
ID_RE = __import__("re").compile(r"^[0-9a-f]{32}$")
SESSION_RE = __import__("re").compile(r"^[0-9a-f-]{36}$")
SPEAKERS = {"Ryan", "Aiden", "Vivian", "Serena"}
ASR_MODEL = os.environ.get("THOZHA_LIVE_ASR_MODEL", "mlx-community/whisper-tiny")
TTS_MODEL_ID = os.environ.get("THOZHA_LIVE_TTS_MODEL", "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit")
CHAT_WS_URL = "ws://127.0.0.1:8000/v1/chat/stream"
CHAT_MODEL = os.environ.get("THOZHA_LIVE_CHAT_MODEL", "gpt-6-luna")
TTS_MODEL: Any = None
TTS_LOAD_MS = 0
ASR_WARM_MS = 0
MLX_LOCK = threading.Lock()


def _now_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _parse_auth_header(connection: ServerConnection) -> str:
    value = connection.request.headers.get("Authorization", "")
    scheme, _, token = value.partition(" ")
    return token if scheme.lower() == "bearer" else ""


class VoiceSession:
    def __init__(self, connection: ServerConnection, device_id: str, session_id: str) -> None:
        self.connection = connection
        self.device_id = device_id
        self.session_id = session_id
        self.input = AudioTurnBuffer(sample_rate=16_000, max_seconds=20)
        self.started = _now_ms()
        self.expected_client_sequence = 0
        self.input_turn_id = 0
        self.last_input_turn_id = -1
        self.turn_task: asyncio.Task | None = None
        self.cancel_flag = threading.Event()
        self.transcript = ""
        self.response_pending = False
        self.output_turn_id = 0
        self.output_sequence = 0

    async def event(self, name: str, **fields: Any) -> None:
        await self.connection.send(json.dumps({"type": name, "session_id": self.session_id, **fields}, separators=(",", ":")))

    async def handle_audio(self, payload: bytes) -> None:
        frame = decode_frame(payload)
        if frame.kind != 1 or frame.sample_rate != 16_000:
            raise VoiceProtocolError("input frame does not match the active format")
        if frame.turn_id < self.input_turn_id:
            return
        if frame.turn_id != self.input_turn_id or frame.sequence != self.expected_client_sequence:
            raise VoiceProtocolError("input frame does not match the active sequence")
        self.input.append(frame)
        self.expected_client_sequence += 1

    async def end_turn(self, input_turn_id: int, final_sequence: int) -> None:
        if input_turn_id < self.input_turn_id:
            await self.event("turn_rejected", input_turn_id=input_turn_id, reason="stale_turn")
            return
        if input_turn_id != self.input_turn_id or final_sequence != self.expected_client_sequence or self.input.size_bytes < 3_200:
            await self.event("turn_rejected", input_turn_id=input_turn_id, reason="empty_or_incomplete")
            self.input.clear()
            self.expected_client_sequence = 0
            return
        if self.turn_task and not self.turn_task.done():
            await self.event("turn_rejected", reason="turn_already_processing")
            return
        pcm = self.input.take()
        self.last_input_turn_id = input_turn_id
        self.input_turn_id += 1
        self.expected_client_sequence = 0
        self.response_pending = True
        self.cancel_flag = threading.Event()
        await self.event("processing", input_turn_id=input_turn_id, frames=final_sequence, bytes=len(pcm), elapsed_ms=_now_ms())
        self.turn_task = asyncio.create_task(self.transcribe(pcm, input_turn_id), name=f"thozha-asr-{self.session_id}")

    async def transcribe(self, pcm: bytes, input_turn_id: int) -> None:
        started = _now_ms()
        try:
            signal = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            def transcribe() -> dict[str, Any]:
                with MLX_LOCK:
                    return mlx_whisper.transcribe(
                        signal,
                        path_or_hf_repo=ASR_MODEL,
                        language="English",
                        condition_on_previous_text=False,
                        verbose=False,
                    )
            result = await asyncio.to_thread(transcribe)
            transcript = str(result.get("text", "")).strip()
            if not transcript:
                self.response_pending = False
                if self.turn_task is asyncio.current_task():
                    self.turn_task = None
                await self.event("no_match", input_turn_id=input_turn_id, elapsed_ms=_now_ms() - started)
                return
            self.transcript = transcript
            if self.turn_task is asyncio.current_task():
                self.turn_task = None
            LOG.info("event=asr_complete session=%s device=%s chars=%d asr_ms=%d", self.session_id, self.device_id, len(transcript), _now_ms() - started)
            await self.event("transcript", input_turn_id=input_turn_id, text=transcript, asr_ms=_now_ms() - started, elapsed_ms=_now_ms())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.exception("event=asr_failed session=%s error_type=%s", self.session_id, type(exc).__name__)
            self.response_pending = False
            if self.turn_task is asyncio.current_task():
                self.turn_task = None
            await self.event("error", code="asr_failed", detail="Speech recognition failed on the Mac.")

    async def respond(self, input_turn_id: int, speaker: str) -> None:
        if not self.transcript or speaker not in SPEAKERS or not self.response_pending or input_turn_id != self.last_input_turn_id:
            await self.event("error", code="invalid_turn_state", detail="The voice turn is not ready for a response.")
            return
        if self.turn_task and not self.turn_task.done():
            await self.event("error", code="turn_not_ready", detail="Speech recognition is still running.")
            return
        self.cancel_flag = threading.Event()
        self.turn_task = asyncio.create_task(self.generate_response(self.transcript, speaker), name=f"thozha-response-{self.session_id}")

    async def speak_local_result(self, input_turn_id: int, text: str, speaker: str) -> None:
        if not self.response_pending or input_turn_id != self.last_input_turn_id or speaker not in SPEAKERS or not text.strip() or len(text) > 2_000:
            await self.event("error", code="invalid_local_speech", detail="The local response is invalid or the turn is no longer active.")
            return
        self.cancel_flag = threading.Event()
        self.turn_task = asyncio.create_task(self.generate_local_speech(text.strip(), speaker), name=f"thozha-local-tts-{self.session_id}")

    async def generate_local_speech(self, text: str, speaker: str) -> None:
        try:
            self.output_turn_id += 1
            self.output_sequence = 0
            await self.event("thinking", output_turn_id=self.output_turn_id, elapsed_ms=_now_ms())
            await self.event("response_text", output_turn_id=self.output_turn_id, text=text, elapsed_ms=_now_ms())
            await self.synthesize_sentence(text, speaker)
            if self.cancel_flag.is_set():
                await self.event("interrupted", output_turn_id=self.output_turn_id, elapsed_ms=_now_ms())
            else:
                await self.event("turn_complete", output_turn_id=self.output_turn_id, route="PHONE_LOCAL_DATA_MAC_TTS", elapsed_ms=_now_ms())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.exception("event=local_tts_failed session=%s error_type=%s", self.session_id, type(exc).__name__)
            await self.event("error", code="tts_failed", detail="The selected Mac voice could not speak this response.")
        finally:
            self.response_pending = False
            self.transcript = ""
            if self.turn_task is asyncio.current_task():
                self.turn_task = None

    async def generate_response(self, transcript: str, speaker: str) -> None:
        started = _now_ms()
        full_text = ""
        sentence_buffer = ""
        first_text_ms: int | None = None
        self.output_turn_id += 1
        self.output_sequence = 0
        try:
            await self.event("thinking", output_turn_id=self.output_turn_id, elapsed_ms=started)
            async with websocket_connect(CHAT_WS_URL, open_timeout=2, close_timeout=1, max_size=1_000_000) as chat:
                await chat.send(json.dumps({
                    "model": CHAT_MODEL,
                    "message": (
                        "You are Thozha, a concise and warm voice assistant. Respond naturally in one or two short sentences. "
                        "Do not execute tools or claim you changed anything.\n\nUser said: " + transcript
                    ),
                    "transient": True,
                }))
                async for raw in chat:
                    if self.cancel_flag.is_set():
                        break
                    try:
                        chunk = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    event_type = chunk.get("type")
                    if event_type == "error":
                        raise RuntimeError("direct OpenJarvis voice stream failed")
                    if event_type == "done":
                        content = str(chunk.get("content", ""))
                        if content and not full_text:
                            full_text = content
                            sentence_buffer = content
                        break
                    token = str(chunk.get("content", "")) if event_type == "chunk" else ""
                    if token:
                        if first_text_ms is None:
                            first_text_ms = _now_ms() - started
                            LOG.info("event=llm_first_text session=%s model=%s first_text_ms=%d", self.session_id, CHAT_MODEL, first_text_ms)
                        full_text += token
                        sentence_buffer += token
                        await self.event("response_text", output_turn_id=self.output_turn_id, text=token, elapsed_ms=_now_ms())
                        if any(mark in sentence_buffer for mark in (".", "?", "!", "\n")) and len(sentence_buffer.strip()) >= 20:
                            await self.synthesize_sentence(sentence_buffer.strip(), speaker)
                            sentence_buffer = ""
            if not self.cancel_flag.is_set() and sentence_buffer.strip():
                await self.synthesize_sentence(sentence_buffer.strip(), speaker)
            if self.cancel_flag.is_set():
                await self.event("interrupted", output_turn_id=self.output_turn_id, elapsed_ms=_now_ms())
            else:
                LOG.info("event=response_complete session=%s device=%s chars=%d response_ms=%d", self.session_id, self.device_id, len(full_text), _now_ms() - started)
                await self.event("turn_complete", output_turn_id=self.output_turn_id, response_chars=len(full_text), elapsed_ms=_now_ms())
        except asyncio.CancelledError:
            self.cancel_flag.set()
            raise
        except Exception as exc:
            LOG.exception("event=response_failed session=%s error_type=%s", self.session_id, type(exc).__name__)
            await self.event("error", code="response_failed", detail="The Mac could not complete this voice response.")
        finally:
            self.response_pending = False
            self.transcript = ""
            if self.turn_task is asyncio.current_task():
                self.turn_task = None

    async def synthesize_sentence(self, text: str, speaker: str) -> None:
        if self.cancel_flag.is_set():
            return
        await self.event("speaking", output_turn_id=self.output_turn_id, speaker=speaker, elapsed_ms=_now_ms())
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[bytes, int] | None] = asyncio.Queue(maxsize=8)
        cancel_flag = self.cancel_flag
        model = TTS_MODEL

        def producer() -> None:
            try:
                with MLX_LOCK:
                    first_chunk_ms: int | None = None
                    synthesis_started = time.perf_counter()
                    for result in model.generate_custom_voice(
                        text=text,
                        speaker=speaker,
                        language="English",
                        instruct="Warm, calm, conversational delivery.",
                        stream=True,
                        streaming_interval=0.64,
                    ):
                        if cancel_flag.is_set():
                            break
                        pcm = np.clip(np.asarray(result.audio).reshape(-1), -1.0, 1.0)
                        payload = (pcm * 32767.0).astype("<i2", copy=False).tobytes()
                        if payload:
                            if first_chunk_ms is None:
                                first_chunk_ms = round((time.perf_counter() - synthesis_started) * 1000)
                                LOG.info("event=tts_first_chunk session=%s speaker=%s first_chunk_ms=%d bytes=%d", self.session_id, speaker, first_chunk_ms, len(payload))
                            future = asyncio.run_coroutine_threadsafe(queue.put((payload, int(result.sample_rate))), loop)
                            future.result(timeout=10)
                    LOG.info("event=tts_stream_end session=%s speaker=%s synth_ms=%d cancelled=%s", self.session_id, speaker, round((time.perf_counter() - synthesis_started) * 1000), cancel_flag.is_set())
            except Exception as exc:
                future = asyncio.run_coroutine_threadsafe(queue.put(None), loop)
                future.result(timeout=2)
                LOG.exception("event=tts_generation_failed session=%s error_type=%s", self.session_id, type(exc).__name__)
                raise
            else:
                future = asyncio.run_coroutine_threadsafe(queue.put(None), loop)
                future.result(timeout=2)

        producer_task = asyncio.create_task(asyncio.to_thread(producer))
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                payload, sample_rate = item
                frame = PcmFrame(2, sample_rate, self.output_turn_id, self.output_sequence, _now_ms(), payload)
                self.output_sequence += 1
                await self.connection.send(encode_frame_bytes(frame))
            await producer_task
        finally:
            if not producer_task.done():
                cancel_flag.set()
                try:
                    await asyncio.wait_for(producer_task, timeout=3)
                except (asyncio.TimeoutError, Exception):
                    pass

    async def interrupt(self) -> None:
        self.cancel_flag.set()
        task = self.turn_task
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=1.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self.turn_task = None
        self.response_pending = False
        self.transcript = ""
        self.last_input_turn_id = -1
        self.input.clear()
        self.expected_client_sequence = 0
        self.cancel_flag = threading.Event()
        await self.event("interrupted", elapsed_ms=_now_ms())

    async def close(self) -> None:
        self.cancel_flag.set()
        task = self.turn_task
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self.input.clear()


def encode_frame_bytes(frame: PcmFrame) -> bytes:
    # Kept local to avoid cross-module import ambiguity when run as a script.
    return encode_frame(frame)


async def handler(connection: ServerConnection, *, db_path: Path) -> None:
    if not valid_loopback_peer(connection.remote_address):
        await connection.close(code=1008, reason="loopback-only voice sessions")
        return
    token = _parse_auth_header(connection)
    device_id = authenticate_paired_device(db_path, token)
    if not device_id:
        await connection.close(code=1008, reason="paired device authentication required")
        return
    session: VoiceSession | None = None
    try:
        first = await asyncio.wait_for(connection.recv(), timeout=5)
        if not isinstance(first, str):
            await connection.close(code=1008, reason="voice session start required")
            return
        start = json.loads(first)
        session_id = str(start.get("session_id", ""))
        if start.get("type") != "start" or start.get("protocol") != 2 or not SESSION_RE.fullmatch(session_id):
            await connection.close(code=1008, reason="invalid voice session start")
            return
        if start.get("device_id") != device_id or start.get("sample_rate") != 16_000 or start.get("channels") != 1:
            await connection.close(code=1008, reason="voice session identity or format mismatch")
            return
        session = VoiceSession(connection, device_id, session_id)
        await session.event("session_ready", protocol=2, sample_rate=16_000, max_turn_seconds=20, tts_load_ms=TTS_LOAD_MS, asr_warm_ms=ASR_WARM_MS)
        LOG.info("event=session_open session=%s device=%s peer=loopback", session_id, device_id)
        async for message in connection:
            if isinstance(message, bytes):
                await session.handle_audio(message)
                continue
            try:
                control = json.loads(message)
            except (ValueError, TypeError):
                await session.event("error", code="invalid_control", detail="Voice control message is malformed.")
                continue
            if control.get("session_id") != session.session_id:
                await session.event("error", code="session_mismatch", detail="Voice control belongs to another session.")
            elif control.get("type") == "end_turn":
                await session.end_turn(int(control.get("input_turn_id", -1)), int(control.get("next_sequence", -1)))
            elif control.get("type") == "respond":
                await session.respond(int(control.get("input_turn_id", -1)), str(control.get("speaker", "")))
            elif control.get("type") == "local_speech":
                await session.speak_local_result(int(control.get("input_turn_id", -1)), str(control.get("text", "")), str(control.get("speaker", "")))
            elif control.get("type") == "local_complete":
                input_turn_id = int(control.get("input_turn_id", -1))
                if session.response_pending and input_turn_id == session.last_input_turn_id:
                    session.response_pending = False
                    session.transcript = ""
                    session.last_input_turn_id = -1
                    await session.event("turn_complete", input_turn_id=input_turn_id, route="PHONE_LOCAL", elapsed_ms=_now_ms())
                else:
                    await session.event("error", code="stale_turn", detail="The local turn is no longer current.")
            elif control.get("type") == "interrupt":
                await session.interrupt()
            elif control.get("type") == "end_session":
                await session.event("session_closed", elapsed_ms=_now_ms())
                break
            else:
                await session.event("error", code="unknown_control", detail="Voice control type is unsupported.")
    except (ConnectionClosed, asyncio.TimeoutError, json.JSONDecodeError):
        pass
    except VoiceProtocolError as exc:
        LOG.info("event=protocol_rejected session=%s device=%s reason=%s", session.session_id if session else "none", device_id, str(exc))
        try:
            await connection.close(code=1008, reason="voice protocol rejected")
        except Exception:
            pass
    except Exception as exc:
        LOG.exception("event=session_failed session=%s device=%s error_type=%s", session.session_id if session else "none", device_id, type(exc).__name__)
    finally:
        if session:
            await session.close()
            LOG.info("event=session_closed session=%s device=%s lifetime_ms=%d", session.session_id, device_id, _now_ms() - session.started)


async def main() -> None:
    global TTS_MODEL, TTS_LOAD_MS, ASR_WARM_MS
    parser = argparse.ArgumentParser()
    parser.add_argument("--supervisor-db", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    try:
        if not ipaddress.ip_address(args.host).is_loopback:
            raise SystemExit("Refusing non-loopback bind; USB reverse is required for this development build.")
    except ValueError as exc:
        raise SystemExit("Host must be a literal loopback address.") from exc
    if not args.supervisor_db.is_file():
        raise SystemExit("Supervisor paired-device database is missing; no listener started.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    loaded_at = time.perf_counter()
    TTS_MODEL = await asyncio.to_thread(load_model, TTS_MODEL_ID)
    TTS_LOAD_MS = round((time.perf_counter() - loaded_at) * 1000)
    LOG.info("event=tts_ready model=%s load_ms=%d speakers=%s", TTS_MODEL_ID, TTS_LOAD_MS, ",".join(sorted(TTS_MODEL.get_supported_speakers())))
    asr_started = time.perf_counter()
    def warm_asr() -> None:
        with MLX_LOCK:
            mlx_whisper.transcribe(
                np.zeros(16_000, dtype=np.float32),
                path_or_hf_repo=ASR_MODEL,
                language="English",
                condition_on_previous_text=False,
                verbose=False,
            )
    await asyncio.to_thread(warm_asr)
    ASR_WARM_MS = round((time.perf_counter() - asr_started) * 1000)
    LOG.info("event=asr_ready model=%s warm_ms=%d", ASR_MODEL, ASR_WARM_MS)
    async with serve(lambda conn: handler(conn, db_path=args.supervisor_db), args.host, args.port, max_size=70_000, ping_interval=15, ping_timeout=15):
        LOG.info("event=listener_ready bind=%s port=%d transport=usb-reverse-only", args.host, args.port)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
