"""Wall clock by default; manually advanced time only in explicit synthetic tests."""
import asyncio
from datetime import datetime, timedelta, timezone

from chain.common.validation import timestamp


class RealClock:
    synthetic = False
    parse = staticmethod(timestamp)

    def datetime(self):
        return datetime.now(timezone.utc)

    def now(self):
        return self.datetime().isoformat()

    async def sleep_until(self, at):
        await asyncio.sleep(max(0, (timestamp(at)-self.datetime()).total_seconds()))


class ManualClock:
    synthetic = True
    parse = staticmethod(timestamp)

    def __init__(self, at, *, test_mode=False):
        if test_mode is not True:
            raise ValueError("Manual clock requires explicit test mode")
        self._now=timestamp(at)
        self._waiters=set()

    def datetime(self):return self._now
    def now(self):return self._now.isoformat()

    def advance(self, at):
        next_time=timestamp(at)
        if next_time<self._now:raise ValueError("Clock cannot move backwards")
        self._now=next_time
        for waiter in tuple(self._waiters):waiter.set()

    def advance_by(self, seconds):
        self.advance((self._now+timedelta(seconds=seconds)).isoformat())

    async def sleep_until(self, at):
        target=timestamp(at);waiter=asyncio.Event();self._waiters.add(waiter)
        try:
            while self._now<target:
                waiter.clear()
                await waiter.wait()
        finally:self._waiters.discard(waiter)
