"""Pure JSON/schema primitives; safe to import from a deterministic Workflow."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import re


def json_value(value, where="value"):
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"{where}: UTF-8 string required") from exc
        return
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            json_value(item, where)
        return
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        for key, item in value.items():
            json_value(key, where)
            json_value(item, where)
        return
    raise ValueError(f"{where}: finite JSON value required")


def safe_audit_id(value):
    """Preserve a valid diagnostic ID; never admit or repair the original input."""
    if type(value) is not str or not value.strip():
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


def safe_audit_reason(reason):
    """Escape invalid Unicode only in rejection diagnostics, keeping hashes strict."""
    if type(reason) is not str:
        return "Invalid input"
    return reason.encode("utf-8", errors="backslashreplace").decode("utf-8")


def canonical_hash(value) -> str:
    json_value(value)
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def fields(value, required, optional=(), where="object") -> dict:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ValueError(f"{where}: object with string keys required")
    missing = set(required) - set(value)
    unknown = set(value) - set(required) - set(optional)
    if missing or unknown:
        raise ValueError(f"{where}: missing={sorted(missing)}, unsupported={sorted(unknown)}")
    return value


def text(value, where="value") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: nonempty string required")
    return value


def strings(value, where="value", *, nonempty=False) -> list:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{where}: {'nonempty ' if nonempty else ''}list required")
    for item in value:
        text(item, where)
    if len(value) != len(set(value)):
        raise ValueError(f"{where}: duplicate values")
    return value


def boolean(value, where="value"):
    if type(value) is not bool:
        raise ValueError(f"{where}: boolean required")


def positive(value, where="value", *, integer=False):
    types = (int,) if integer else (int, float)
    if type(value) not in types or (type(value) is float and not math.isfinite(value)) or value <= 0:
        raise ValueError(f"{where}: positive {'integer' if integer else 'number'} required")


def timestamp(value, where="time") -> datetime:
    text(value, where)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{where}: ISO 8601 timestamp required") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{where}: timezone required")
    return parsed


def hash_value(value, where="hash") -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError(f"{where}: sha256 content hash required")
    return value
