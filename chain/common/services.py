"""Worker-local capability objects; not serialized into a Workflow."""
import copy
from threading import RLock


class FixtureError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class SummaryCache:
    def __init__(self):
        self._values = {}
        self._lock = RLock()

    def get_or_create(self, key, factory):
        with self._lock:
            hit = key in self._values
            if not hit:
                self._values[key] = copy.deepcopy(factory())
            return copy.deepcopy(self._values[key]), hit


class AgentServices:
    def __init__(self, cache, backend):
        self.cache = cache
        self.backend = backend
