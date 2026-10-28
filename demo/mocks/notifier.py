"""Persistent mock receipts only; never sends a hospital notification."""
import json
import copy
from pathlib import Path
import sqlite3
from contextlib import contextmanager

from chain.common.validation import canonical_hash, fields, strings, text, timestamp


class MockNotifier:
    def __init__(self, path, templates, *, write=print):
        self.path = str(Path(path))
        self.templates = frozenset(strings(templates))
        self.write = write
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS receipts (request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, receipt TEXT NOT NULL)")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def record(self, request, *, recorded_at):
        request = copy.deepcopy(request)
        fields(request, {"request_schema", "request_id", "episode_id", "template", "channel", "recipient"}, where="Notification request")
        if request["request_schema"] != "chain-notification/v0.3":
            raise ValueError("Unsupported notification schema")
        for key in ("request_id", "episode_id", "template", "channel", "recipient"):
            text(request[key])
        if request["template"] not in self.templates or request["channel"] not in {"CONSOLE", "LOG"}:
            raise ValueError("Unapproved template or unsupported mock channel")
        timestamp(recorded_at)
        commitment = canonical_hash(request)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload_hash, receipt FROM receipts WHERE request_id=?", (request["request_id"],)).fetchone()
            if existing:
                if existing[0] != commitment:
                    raise ValueError("Notification request_id reused with different payload")
                receipt = json.loads(existing[1])
            else:
                receipt = {"receipt_schema": "chain-notification-receipt/v0.3", "request_id": request["request_id"],
                           "episode_id": request["episode_id"], "payload_hash": commitment,
                           **{k:request[k] for k in ("template", "channel", "recipient")},
                           "status": "MOCK_RECORDED", "recorded_at": recorded_at}
                db.execute("INSERT INTO receipts VALUES (?, ?, ?)", (request["request_id"], commitment, json.dumps(receipt)))
        # A logging failure after commit is safe to retry; the durable effect stays singular.
        self.write("MOCK_RECORDED " + json.dumps(receipt, sort_keys=True))
        return receipt

    def receipts(self):
        with self._connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT receipt FROM receipts ORDER BY request_id")]
