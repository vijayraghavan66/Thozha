"""Small, bounded PCM frame codec used by Thozha Live's USB/LAN-neutral stream."""
from __future__ import annotations

from dataclasses import dataclass
import struct

MAGIC = b"THZV"
VERSION = 1
HEADER = struct.Struct(">4sBBHIQ")
MAX_PCM_BYTES = 64 * 1024
MAX_AUDIO_SECONDS = 20


class VoiceProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class PcmFrame:
    kind: int
    sample_rate: int
    sequence: int
    elapsed_ms: int
    pcm: bytes


def encode_frame(frame: PcmFrame) -> bytes:
    validate_frame(frame)
    return HEADER.pack(MAGIC, VERSION, frame.kind, frame.sample_rate, frame.sequence, frame.elapsed_ms) + frame.pcm


def decode_frame(payload: bytes) -> PcmFrame:
    if len(payload) < HEADER.size:
        raise VoiceProtocolError("frame header is truncated")
    magic, version, kind, sample_rate, sequence, elapsed_ms = HEADER.unpack_from(payload)
    if magic != MAGIC or version != VERSION:
        raise VoiceProtocolError("frame protocol version is invalid")
    frame = PcmFrame(kind, sample_rate, sequence, elapsed_ms, payload[HEADER.size:])
    validate_frame(frame)
    return frame


def validate_frame(frame: PcmFrame) -> None:
    if frame.kind not in (1, 2):
        raise VoiceProtocolError("frame kind is unsupported")
    if frame.sample_rate not in (16_000, 24_000, 48_000):
        raise VoiceProtocolError("sample rate is unsupported")
    if not 0 <= frame.sequence <= 0xFFFFFFFF:
        raise VoiceProtocolError("frame sequence is outside the protocol range")
    if not 0 <= frame.elapsed_ms <= 0xFFFFFFFFFFFFFFFF:
        raise VoiceProtocolError("frame timestamp is outside the protocol range")
    if not frame.pcm or len(frame.pcm) > MAX_PCM_BYTES or len(frame.pcm) % 2:
        raise VoiceProtocolError("PCM payload size is invalid")


class AudioTurnBuffer:
    """In-memory mono s16le turn buffer with strict sequencing and duration bounds."""

    def __init__(self, *, sample_rate: int = 16_000, max_seconds: int = MAX_AUDIO_SECONDS) -> None:
        if sample_rate not in (16_000, 24_000, 48_000) or not 1 <= max_seconds <= MAX_AUDIO_SECONDS:
            raise ValueError("invalid voice buffer limits")
        self.sample_rate = sample_rate
        self.max_bytes = sample_rate * 2 * max_seconds
        self._data = bytearray()
        self._next_sequence = 0

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    @property
    def size_bytes(self) -> int:
        return len(self._data)

    def append(self, frame: PcmFrame) -> None:
        if frame.kind != 1 or frame.sample_rate != self.sample_rate:
            raise VoiceProtocolError("input frame format does not match the session")
        if frame.sequence != self._next_sequence:
            raise VoiceProtocolError("input frame is duplicate or out of order")
        if len(self._data) + len(frame.pcm) > self.max_bytes:
            raise VoiceProtocolError("audio turn exceeded its duration limit")
        self._data.extend(frame.pcm)
        self._next_sequence += 1

    def take(self) -> bytes:
        result = bytes(self._data)
        self.clear()
        return result

    def clear(self) -> None:
        self._data.clear()
        self._next_sequence = 0

