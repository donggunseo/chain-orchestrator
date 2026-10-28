"""In-memory published-only Store. Private source paths never enter this Store."""
import copy
from threading import RLock

from chain.common.contracts import source_ref
from chain.common.source_contracts import identity, validate_initial, validate_record
from chain.common.validation import hash_value, timestamp


class NotPublishedError(ValueError):
    pass


class PublishedStore:
    def __init__(self, initial):
        validate_initial(initial)
        self.identity = identity(initial)
        self._records = {}
        self._lock = RLock()

    @staticmethod
    def _key(ref):
        source_ref(ref)
        return ref["system"], ref["record_id"], ref["version"]

    def publish(self, record, *, at):
        validate_record(record)
        if identity(record) != self.identity:
            raise ValueError("Source identity mismatch")
        if timestamp(at) < timestamp(record["source_time"]):
            raise NotPublishedError("Source has not yet occurred/saved")
        key = self._key(record["source_ref"])
        with self._lock:
            existing = self._records.get(key)
            if existing:
                if existing["record"]["content_hash"] != record["content_hash"]:
                    raise ValueError("Conflicting content for the same source version")
                if timestamp(at) < timestamp(existing["published_at"]):
                    raise NotPublishedError("Cannot backdate an existing publication")
                return copy.deepcopy(existing)
            entry = {"record": copy.deepcopy(record), "published_at": at}
            self._records[key] = entry
            return copy.deepcopy(entry)

    def resolve(self, ref, *, at, content_hash):
        key = self._key(ref); hash_value(content_hash); now = timestamp(at)
        with self._lock:
            entry = self._records.get(key)
            if entry is None or now < timestamp(entry["published_at"]):
                raise NotPublishedError("Exact source version is not published at requested time")
            if entry["record"]["content_hash"] != content_hash:
                raise ValueError("Published source content hash mismatch")
            return copy.deepcopy(entry)
