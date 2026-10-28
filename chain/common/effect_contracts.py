"""Pure contracts for mock presentation effects; safe inside a Workflow.

These receipts describe a local mock record. They never assert an EHR write,
message delivery, or deletion from an external system.
"""

from .validation import (boolean, canonical_hash, fields, hash_value, json_value,
                         positive, text, timestamp)


OPERATIONS = frozenset({"SET_FLAG", "CLEAR_FLAG", "SET_DASHBOARD", "CLEAR_DASHBOARD"})
REQUEST_FIELDS = frozenset({"request_schema", "request_id", "episode_id", "operation",
                            "resource", "target", "effect_version"})
BOUND_FIELDS = ("request_id", "episode_id", "operation", "resource", "target", "effect_version")
RECEIPT_FIELDS = frozenset({"receipt_schema", *BOUND_FIELDS, "payload_hash", "status",
                            "recorded_at", "projection_updated"})


def validate_effect_request(request):
    fields(request, REQUEST_FIELDS, where="Presentation effect request")
    json_value(request, "Presentation effect request")
    if request["request_schema"] != "chain-effect/v0.3":
        raise ValueError("Unsupported presentation effect request schema")
    for key in ("request_id", "episode_id", "operation", "resource", "target"):
        text(request[key], key)
    if request["operation"] not in OPERATIONS:
        raise ValueError("Unsupported presentation effect operation")
    positive(request["effect_version"], "effect_version", integer=True)


def validate_effect_receipt(receipt, request):
    validate_effect_request(request)
    fields(receipt, RECEIPT_FIELDS, where="Presentation effect receipt")
    json_value(receipt, "Presentation effect receipt")
    if receipt["receipt_schema"] != "chain-effect-receipt/v0.3":
        raise ValueError("Unsupported presentation effect receipt schema")
    for key in ("request_id", "episode_id", "operation", "resource", "target"):
        text(receipt[key], key)
    positive(receipt["effect_version"], "effect_version", integer=True)
    for key in BOUND_FIELDS:
        if receipt[key] != request[key]:
            raise ValueError(f"Presentation effect receipt {key} mismatch")
    hash_value(receipt["payload_hash"], "effect payload_hash")
    if receipt["payload_hash"] != canonical_hash(request):
        raise ValueError("Presentation effect receipt payload_hash mismatch")
    if receipt["status"] != "MOCK_RECORDED":
        raise ValueError("Presentation effects support MOCK_RECORDED receipts only")
    timestamp(receipt["recorded_at"], "recorded_at")
    boolean(receipt["projection_updated"], "projection_updated")
