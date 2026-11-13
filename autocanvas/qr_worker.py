"""Disposable live QR decoder. OpenCV/native allocations stay out of the API."""
import asyncio
import json
import struct
import sys

MAX_FRAME = 16 * 1024 * 1024
MAX_REPLY = 1024 * 1024


class ProcessQRDetector:
    """One serial decoder per live view; the async owner must call close()."""
    def __init__(self, *, timeout=15):
        self.timeout = timeout
        self.process = None

    async def _start(self):
        # Join a cancelled spawn too: cancellation must not lose a child's handle.
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable, '-m', 'autocanvas.qr_worker',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=MAX_REPLY))
        try:
            self.process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            self.process = await spawning
            raise

    async def detect_bytes(self, data):
        if not 0 < len(data) <= MAX_FRAME:
            raise ValueError('Invalid QR frame size')
        try:
            async with asyncio.timeout(self.timeout):
                if self.process is None:
                    await self._start()
                self.process.stdin.write(struct.pack('!I', len(data)))
                self.process.stdin.write(data)
                await self.process.stdin.drain()
                reply = await self.process.stdout.readline()
                if not reply:
                    raise RuntimeError('QR worker exited')
                result = json.loads(reply)
                if not result['ok']:
                    raise RuntimeError('QR worker failed: '+result['error'])
                return result['rows']
        except BaseException:
            await self.close()
            raise

    async def close(self):
        process, self.process = self.process, None
        if process is None:
            return

        async def reap():
            # Drain any in-flight response so a full output pipe cannot block wait().
            async def drain():
                while await process.stdout.read(65536):
                    pass
            draining = asyncio.create_task(drain())
            try:
                if process.stdin:
                    process.stdin.close()
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    await process.wait()
            finally:
                await draining

        cleanup = asyncio.create_task(reap())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


def _serve():
    from .qr import QRDetector
    detector = QRDetector()
    # Parallel live views already have separate processes. Avoid multiplying
    # OpenCV's native thread pools and their retained allocator arenas.
    detector.cv2.setNumThreads(1)
    incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer
    while header := incoming.read(4):
        if len(header) != 4:
            return
        size, = struct.unpack('!I', header)
        if not 0 < size <= MAX_FRAME:
            return
        data = incoming.read(size)
        if len(data) != size:
            return
        try:
            result = {'ok': True, 'rows': detector.detect_bytes(data)}
            reply = json.dumps(result, ensure_ascii=True).encode()+b'\n'
            if len(reply) > MAX_REPLY:
                raise ValueError('QR reply exceeds limit')
        except Exception as error:
            # Neither image contents nor decoded credentials enter diagnostics.
            reply = json.dumps({'ok': False, 'error': type(error).__name__}).encode()+b'\n'
            outgoing.write(reply)
            outgoing.flush()
            return
        outgoing.write(reply)
        outgoing.flush()


if __name__ == '__main__':
    _serve()
