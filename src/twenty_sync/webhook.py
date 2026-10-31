"""
HMAC-verified Apify Webhook handler (`ACTOR.RUN.SUCCEEDED`).
Fetches clean dataset items from Apify API v2 and triggers `TwentySyncEngine`.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from .client import TwentySyncEngine
from .models import SyncRunSummary


def verify_apify_webhook_signature(
    raw_body: bytes, signature_header: str | None, secret: str
) -> bool:
    """
    Constant-time HMAC-SHA256 verification for incoming Apify webhook requests
    (`X-Apify-Webhook-Signature` or `sha256=<hex>`).
    """
    if not secret or not signature_header:
        return False
    provided = signature_header.strip()
    if provided.lower().startswith("sha256="):
        provided = provided.split("=", 1)[1].strip()
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.lower())


async def fetch_apify_dataset_items(
    dataset_id: str,
    *,
    apify_token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if apify_token:
        headers["Authorization"] = f"Bearer {apify_token}"
    url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"
    async with httpx.AsyncClient(timeout=30.0, headers=headers, transport=transport) as http:
        resp = await http.get(url, params={"clean": "true", "format": "json"})
        resp.raise_for_status()
        items = resp.json()
        return items if isinstance(items, list) else []


async def handle_apify_webhook_event(
    event_payload: dict[str, Any],
    *,
    engine: TwentySyncEngine,
    apify_token: str | None = None,
    apify_transport: httpx.AsyncBaseTransport | None = None,
) -> SyncRunSummary:
    """
    Process an Apify `ACTOR.RUN.SUCCEEDED` webhook event payload:
    `{"eventType": "ACTOR.RUN.SUCCEEDED", "resource": {"defaultDatasetId": "..."}}`
    """
    event_type = str(event_payload.get("eventType") or "")
    if event_type != "ACTOR.RUN.SUCCEEDED":
        return SyncRunSummary(mode=f"ignored:{event_type or 'unknown'}")

    resource = event_payload.get("resource") or {}
    dataset_id = resource.get("defaultDatasetId") or event_payload.get("datasetId")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise ValueError("Missing `resource.defaultDatasetId` in Apify webhook payload.")

    items = await fetch_apify_dataset_items(
        dataset_id.strip(), apify_token=apify_token, transport=apify_transport
    )
    summary, _ = await engine.sync_items(items, mode_label="apify-webhook")
    return summary
