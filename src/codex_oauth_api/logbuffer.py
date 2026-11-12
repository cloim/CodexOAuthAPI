from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from threading import Lock
from typing import Any

LOG_BUFFER_SIZE = 500
_MAX_DETAIL_CHARS = 4000


class LogBuffer:
    """Thread-safe ring buffer holding the most recent server log records for the dashboard."""

    def __init__(self, maxlen: int = LOG_BUFFER_SIZE) -> None:
        self._records: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = Lock()
        self._next_id = 1

    def add(self, level: str, log_type: str, message: str, detail: Any = None) -> dict[str, Any]:
        rendered_detail: str | None = None
        if detail is not None:
            rendered_detail = (
                detail
                if isinstance(detail, str)
                else json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)
            )
            if len(rendered_detail) > _MAX_DETAIL_CHARS:
                rendered_detail = rendered_detail[:_MAX_DETAIL_CHARS] + "..."
        with self._lock:
            record = {
                "id": self._next_id,
                "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "level": level,
                "type": log_type,
                "message": message,
                "detail": rendered_detail,
            }
            self._next_id += 1
            self._records.append(record)
            return record

    def list(
        self,
        *,
        query: str | None = None,
        level: str | None = None,
        log_type: str | None = None,
        after: int = 0,
        limit: int = LOG_BUFFER_SIZE,
    ) -> list[dict[str, Any]]:
        needle = query.lower() if query else None
        with self._lock:
            snapshot = list(self._records)
        matched = []
        for record in snapshot:
            if record["id"] <= after:
                continue
            if level and record["level"] != level:
                continue
            if log_type and record["type"] != log_type:
                continue
            if needle and needle not in f"{record['message']} {record['detail'] or ''}".lower():
                continue
            matched.append(record)
        return matched[-limit:] if limit > 0 else matched

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


LOG_BUFFER = LogBuffer()
