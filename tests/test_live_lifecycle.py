import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from autocanvas.live_attendance import LiveAttendance
from autocanvas.storage import Store
from autocanvas.types import MediaSource


class LiveLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'state.sqlite3')

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def observer(self, *, end_seconds=10):
        self.started = {name: asyncio.Event() for name in ('fast', 'slow')}
        self.cleaned = set()
        self.interrupted = set()
        class Detector:
            def detect_bytes(self, data):
                return []
        async def reader(source):
            try:
                yield b'frame'
                self.started[source.view].set()
                await asyncio.Event().wait()
            finally:
                try:
                    if source.view == 'slow':
                        await asyncio.sleep(.05)
                    self.cleaned.add(source.view)
                except asyncio.CancelledError:
                    self.interrupted.add(source.view)
                    raise
        async def resolve(lecture, protocol):
            return [MediaSource('fixture', view=name) for name in ('fast', 'slow')]
        observer = LiveAttendance(self.store, self.root, resolve, SimpleNamespace(), reader=reader, detector_factory=Detector)
        lecture = {'course_id': '10001', 'id': 'lecture', 'kind': 'live',
                   'end': (datetime.now(timezone.utc)+timedelta(seconds=end_seconds)).isoformat()}
        return observer, lecture

    async def test_cancel_multiple_views_finishes_every_reader_and_clears_status(self):
        observer, lecture = self.observer()
        task = asyncio.create_task(observer.lecture(lecture))
        for started in self.started.values():
            await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        self.assertFalse(observer.views)
        self.assertEqual(self.cleaned, {'fast', 'slow'})
        self.assertFalse(self.interrupted)
        self.assertFalse(observer.worker.latest)

    async def test_class_end_closes_views_without_waiting_for_outer_scheduler(self):
        observer, lecture = self.observer(end_seconds=.08)
        task = asyncio.create_task(observer.lecture(lecture))
        try:
            await asyncio.wait_for(task, 1)
            self.assertFalse(observer.views)
            self.assertEqual(self.cleaned, {'fast', 'slow'})
            self.assertFalse(self.interrupted)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_expired_class_does_not_resolve_or_reconnect(self):
        observer, lecture = self.observer(end_seconds=-1)
        called = []
        async def resolve(*args):
            called.append(True)
            return []
        observer.resolve = resolve
        task = asyncio.create_task(observer.lecture(lecture))
        try:
            await asyncio.wait_for(task, .2)
            self.assertFalse(called)
            self.assertFalse(observer.views)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_real_multiple_ffmpeg_readers_are_reaped_on_cancel(self):
        import subprocess
        from unittest.mock import patch
        from autocanvas import media
        video = self.root/'fixture.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-hide_banner', '-loglevel', 'error',
                        '-f', 'lavfi', '-i', 'color=size=160x120:rate=10', '-t', '2',
                        '-c:v', 'libx264', '-preset', 'ultrafast', str(video)], check=True)
        class Detector:
            def detect_bytes(self, data):
                return []
        async def resolve(*args):
            return [MediaSource(str(video), view=str(i)) for i in range(2)]
        processes = []
        spawn = asyncio.create_subprocess_exec
        async def tracked_spawn(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            processes.append(process)
            return process
        observer = LiveAttendance(self.store, self.root, resolve, SimpleNamespace(), detector_factory=Detector)
        lecture = {'course_id': '10001', 'id': 'lecture',
                   'end': (datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()}
        with patch.object(media, 'inputs', return_value=['-re', '-stream_loop', '-1']), \
                patch('autocanvas.media.asyncio.create_subprocess_exec', side_effect=tracked_spawn):
            task = asyncio.create_task(observer.lecture(lecture))
            try:
                async with asyncio.timeout(8):
                    while len(observer.views) != 2 or not all(v['last_frame'] for v in observer.views.values()):
                        await asyncio.sleep(.01)
                task.cancel()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 8)
                self.assertEqual(len(processes), 2)
                self.assertTrue(all(p.returncode is not None for p in processes))
                self.assertFalse(observer.views)
                self.assertFalse(observer.worker.latest)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
