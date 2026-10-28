"""Worker-owned SQLite mock records for flags and dashboard presentation.

No hospital API or notification transport is called. A projection is only this
mock's latest desired display state, ordered by the supplied request sequence.
"""

import copy
import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from threading import RLock

from chain.common.effect_contracts import (BOUND_FIELDS, OPERATIONS, validate_effect_receipt,
                              validate_effect_request)
from chain.common.validation import canonical_hash, text, timestamp


class MockPresentationEffects:
    def __init__(self, path, allowed_operations=OPERATIONS, *, write=print):
        if isinstance(allowed_operations, str):
            raise ValueError("Effect authorization requires an operation collection")
        try:
            operations = frozenset(allowed_operations)
        except TypeError as exc:
            raise ValueError("Effect authorization requires an operation collection") from exc
        if not operations.issubset(OPERATIONS):
            raise ValueError("Effect authorization contains unsupported operations")
        self.path = str(Path(path))
        self._operations = operations
        self.write = write
        self._lock = RLock()
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS effects_receipts (
                request_id TEXT PRIMARY KEY,
                payload_hash TEXT NOT NULL,
                receipt TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS effect_projection (
                episode_id TEXT NOT NULL,
                target TEXT NOT NULL,
                resource TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('FLAG', 'DASHBOARD')),
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                effect_version TEXT NOT NULL,
                PRIMARY KEY (episode_id, target, resource, kind)
            )""")

    @property
    def operations(self):
        """Immutable operation authorization inspected by the Worker."""
        return self._operations

    @contextmanager
    def _connect(self):
        with self._lock:
            connection = sqlite3.connect(self.path, timeout=10)
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    def record(self, request, *, recorded_at):
        request = copy.deepcopy(request)
        validate_effect_request(request)
        if request["operation"] not in self.operations:
            raise ValueError("Presentation effect operation not authorized")
        timestamp(recorded_at, "recorded_at")
        commitment = canonical_hash(request)
        kind = "FLAG" if request["operation"].endswith("_FLAG") else "DASHBOARD"
        enabled = request["operation"].startswith("SET_")
        key = (request["episode_id"], request["target"], request["resource"], kind)
        version = request["effect_version"]
        with self._connect() as db:
            # Serialize the duplicate check, version check, projection and receipt
            # across both threads and independent Worker instances.
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT payload_hash, receipt FROM effects_receipts WHERE request_id=?",
                (request["request_id"],),
            ).fetchone()
            if existing:
                if existing[0] != commitment:
                    raise ValueError("Effect request_id reused with different payload")
                receipt = json.loads(existing[1])
                validate_effect_receipt(receipt, request)
            else:
                projection = db.execute(
                    """SELECT enabled, effect_version FROM effect_projection
                       WHERE episode_id=? AND target=? AND resource=? AND kind=?""",
                    key,
                ).fetchone()
                if projection and version == int(projection[1]) and enabled != bool(projection[0]):
                    raise ValueError("Conflicting presentation effect at the same effect_version")
                if projection and version < int(projection[1]):
                    # A newer projection does not erase the intent committed
                    # for an older request version. Conflicting late requests
                    # for that same old version must also be rejected.
                    for (stored,) in db.execute("SELECT receipt FROM effects_receipts"):
                        prior = json.loads(stored)
                        prior_key = (prior["episode_id"], prior["target"], prior["resource"],
                                     "FLAG" if prior["operation"].endswith("_FLAG") else "DASHBOARD")
                        if prior_key == key and prior["effect_version"] == version:
                            if prior["operation"].startswith("SET_") != enabled:
                                raise ValueError("Conflicting presentation effect at the same effect_version")
                            break
                updated = projection is None or version > int(projection[1])
                if updated:
                    # Store decimal text so the contract's positive integer is
                    # not silently narrowed to SQLite's signed 64-bit range.
                    db.execute(
                        """INSERT INTO effect_projection
                           (episode_id, target, resource, kind, enabled, effect_version)
                           VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(episode_id, target, resource, kind) DO UPDATE SET
                               enabled=excluded.enabled, effect_version=excluded.effect_version""",
                        (*key, int(enabled), str(version)),
                    )
                receipt = {
                    "receipt_schema": "chain-effect-receipt/v0.3",
                    **{field: request[field] for field in BOUND_FIELDS},
                    "payload_hash": commitment,
                    "status": "MOCK_RECORDED",
                    "recorded_at": recorded_at,
                    "projection_updated": updated,
                }
                validate_effect_receipt(receipt, request)
                db.execute(
                    "INSERT INTO effects_receipts (request_id, payload_hash, receipt) VALUES (?, ?, ?)",
                    (request["request_id"], commitment, json.dumps(receipt, sort_keys=True)),
                )
        # Logging may repeat on retry. Its failure after commit cannot duplicate
        # the durable receipt or undo a newer projection.
        self.write("MOCK_RECORDED " + json.dumps(receipt, sort_keys=True))
        return receipt

    def receipts(self):
        with self._connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT receipt FROM effects_receipts ORDER BY request_id"
            )]

    def projections(self, episode_id=None):
        if episode_id is not None:
            text(episode_id, "episode_id")
        query = "SELECT episode_id, target, resource, kind, enabled, effect_version FROM effect_projection"
        parameters = ()
        if episode_id is not None:
            query += " WHERE episode_id=?"
            parameters = (episode_id,)
        query += " ORDER BY episode_id, target, resource, kind"
        with self._connect() as db:
            return [{"episode_id": row[0], "target": row[1], "resource": row[2],
                     "kind": row[3], "enabled": bool(row[4]), "effect_version": int(row[5])}
                    for row in db.execute(query, parameters)]
