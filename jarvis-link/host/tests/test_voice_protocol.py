import pytest

from jarvis_link_host.voice_protocol import (
    AudioTurnBuffer,
    PcmFrame,
    VoiceProtocolError,
    decode_frame,
    encode_frame,
)


def test_frame_round_trip_preserves_sequence_and_pcm():
    frame = PcmFrame(kind=1, sample_rate=16_000, sequence=23, elapsed_ms=987654, pcm=b"\x01\x00" * 320)
    assert decode_frame(encode_frame(frame)) == frame


def test_turn_buffer_rejects_duplicate_and_out_of_order_frames():
    buffer = AudioTurnBuffer(max_seconds=1)
    frame = PcmFrame(1, 16_000, 0, 1, b"\x00\x00" * 320)
    buffer.append(frame)
    with pytest.raises(VoiceProtocolError, match="duplicate or out of order"):
        buffer.append(frame)
    with pytest.raises(VoiceProtocolError, match="duplicate or out of order"):
        buffer.append(PcmFrame(1, 16_000, 3, 2, b"\x00\x00" * 320))


def test_turn_buffer_enforces_pcm_and_duration_limits():
    with pytest.raises(VoiceProtocolError, match="payload size"):
        encode_frame(PcmFrame(1, 16_000, 0, 0, b"\x00"))
    buffer = AudioTurnBuffer(max_seconds=1)
    for seq in range(50):
        buffer.append(PcmFrame(1, 16_000, seq, seq, b"\x00\x00" * 320))
    with pytest.raises(VoiceProtocolError, match="duration limit"):
        buffer.append(PcmFrame(1, 16_000, 50, 50, b"\x00\x00" * 320))
    assert len(buffer.take()) == 16_000 * 2
    assert buffer.size_bytes == 0


def test_frame_rejects_unknown_header_and_sample_rate():
    encoded = bytearray(encode_frame(PcmFrame(1, 16_000, 0, 0, b"\x00\x00" * 3)))
    encoded[0] = ord("X")
    with pytest.raises(VoiceProtocolError, match="protocol version"):
        decode_frame(bytes(encoded))
    with pytest.raises(VoiceProtocolError, match="sample rate"):
        encode_frame(PcmFrame(1, 22_050, 0, 0, b"\x00\x00" * 3))
