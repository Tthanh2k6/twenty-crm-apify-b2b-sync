"""
Append-only JSONL idempotent audit ledger (`sync_audit.jsonl`).
Prevents duplicate writes across repeated Apify dataset syncs or webhook retries.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any


class SyncAuditLedger:
    """Stores deterministic SHA-256 idempotency fingerprints of synced leads."""

    def __init__(self, path: str | Path | None) -> None:
        self.path: Path | None = Path(path) if path else None
        self._seen_keys: dict[str, dict[str, Any]] = {}
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                key = entry.get("idempotency_key")
                if isinstance(key, str) and key:
                    self._seen_keys[key] = entry
            except json.JSONDecodeError:
                continue

    def has_key(self, idempotency_key: str) -> bool:
        return idempotency_key in self._seen_keys

    def get_entry(self, idempotency_key: str) -> dict[str, Any] | None:
        return self._seen_keys.get(idempotency_key)

    def record(
        self,
        *,
        idempotency_key: str,
        canonical_domain: str,
        company_id: str,
        person_id: str | None,
        note_id: str | None,
        opportunity_id: str | None,
        action: str,
    ) -> dict[str, Any]:
        entry = {
            "idempotency_key": idempotency_key,
            "canonical_domain": canonical_domain,
            "company_id": company_id,
            "person_id": person_id,
            "note_id": note_id,
            "opportunity_id": opportunity_id,
            "action": action,
            "synced_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        self._seen_keys[idempotency_key] = entry
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry
