import asyncio
import subprocess
import sys
import unittest
from unittest.mock import patch

from autocanvas.qr_worker import ProcessQRDetector


class QRWorkerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Generate the fixture outside this interpreter too, so isolation is tested.
        cls.image = subprocess.check_output([sys.executable, '-c', '''
import cv2, sys
qr = cv2.QRCodeEncoder_create().encode('https://example.org/isolated-qr')
qr = cv2.copyMakeBorder(qr, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
qr = cv2.resize(qr, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
sys.stdout.buffer.write(cv2.imencode('.jpg', qr)[1].tobytes())
'''])

    async def test_real_decode_isolated_and_repeated_classes_reap_children(self):
        imported_before = {name for name in ('cv2', 'numpy') if name in sys.modules}
        for _ in range(3):
            worker = ProcessQRDetector()
            try:
                for _ in range(3):
                    rows = await worker.detect_bytes(self.image)
                    self.assertEqual(rows[0]['content'], 'https://example.org/isolated-qr')
                process = worker.process
                self.assertNotEqual(process.pid, __import__('os').getpid())
                self.assertEqual({name for name in ('cv2', 'numpy') if name in sys.modules}, imported_before)
            finally:
                await worker.close()
            self.assertIsNotNone(process.returncode)
            self.assertIsNone(worker.process)

    async def hung_worker(self):
        # A real process with no protocol replies exercises timeout/pipe cleanup.
        spawn = asyncio.create_subprocess_exec
        async def hung(*args, **kwargs):
            return await spawn(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
        return hung

    async def test_timeout_reaps_unresponsive_child(self):
        worker = ProcessQRDetector(timeout=.2)
        with patch('autocanvas.qr_worker.asyncio.create_subprocess_exec', await self.hung_worker()):
            task = asyncio.create_task(worker.detect_bytes(self.image))
            while worker.process is None:
                await asyncio.sleep(.001)
            process = worker.process
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(task, 5)
        self.assertIsNotNone(process.returncode)
        self.assertIsNone(worker.process)

    async def test_cancel_inflight_decode_reaps_child(self):
        worker = ProcessQRDetector()
        with patch('autocanvas.qr_worker.asyncio.create_subprocess_exec', await self.hung_worker()):
            task = asyncio.create_task(worker.detect_bytes(self.image))
            while worker.process is None:
                await asyncio.sleep(.001)
            process = worker.process
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        self.assertIsNotNone(process.returncode)

    async def test_cancel_during_spawn_does_not_lose_child(self):
        spawning, release = asyncio.Event(), asyncio.Event()
        spawn = asyncio.create_subprocess_exec
        processes = []
        async def delayed(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            processes.append(process)
            spawning.set()
            await release.wait()
            return process
        worker = ProcessQRDetector()
        with patch('autocanvas.qr_worker.asyncio.create_subprocess_exec', side_effect=delayed):
            task = asyncio.create_task(worker.detect_bytes(self.image))
            await asyncio.wait_for(spawning.wait(), 5)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        self.assertIsNotNone(processes[0].returncode)
        self.assertIsNone(worker.process)

    async def test_crashed_child_is_reaped_and_next_request_can_restart(self):
        worker = ProcessQRDetector()
        try:
            await worker.detect_bytes(self.image)
            process = worker.process
            process.kill()
            await process.wait()
            with self.assertRaises((BrokenPipeError, ConnectionResetError, RuntimeError)):
                await worker.detect_bytes(self.image)
            self.assertIsNone(worker.process)
            self.assertTrue(await worker.detect_bytes(self.image))
        finally:
            await worker.close()

    async def test_invalid_input_is_bounded_without_starting_worker(self):
        worker = ProcessQRDetector()
        with self.assertRaises(ValueError):
            await worker.detect_bytes(b'')
        self.assertIsNone(worker.process)
