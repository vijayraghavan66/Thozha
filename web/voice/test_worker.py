"""No-network regressions for cancelled and malformed recognition requests."""
import base64
import importlib.util
import io
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import wave

spec = importlib.util.spec_from_file_location('voice_worker', Path(__file__).with_name('worker.py'))
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
# worker reserves stdout for protocol only in production.
import sys
sys.stdout = worker.protocol

class Recognition(unittest.TestCase):
    def test_cancelled_asr_does_not_call_backend(self):
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(bytes(3200))
        worker.cancelled.add('cancelled')
        with patch('httpx.Client') as client:
            worker.transcribe({'id':'cancelled', 'audio':base64.b64encode(buf.getvalue()).decode()})
            client.assert_not_called()
        worker.cancelled.clear()

    def test_slot_released_on_decode_error(self):
        worker.asr_slots = threading.BoundedSemaphore(1)
        worker.asr_slots.acquire()
        with patch.object(worker, 'transcribe', side_effect=ValueError()), patch.object(worker, 'emit'):
            worker.run_asr({'id':'bad'})
        self.assertTrue(worker.asr_slots.acquire(blocking=False))
        worker.asr_slots.release()

if __name__ == '__main__': unittest.main()
