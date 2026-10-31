"""
Async Twenty CRM REST Client (`httpx.AsyncClient`) + Stateful In-Memory Mock Transport
for `--demo-mock` verification and offline integration tests.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Callable, Coroutine
from urllib.parse import parse_qs, urlparse

import httpx

from .ledger import SyncAuditLedger
from .models import (
    ApifyEnrichedLead,
    ApifySocialProfiles,
    CompanyUpsertPayload,
    NoteCreatePayload,
    NoteTargetCreatePayload,
    OpportunityCreatePayload,
    PersonUpsertPayload,
    SyncRunSummary,
)
from .normalizer import (
    build_company_payload,
    build_enrichment_note_payload,
    build_opportunity_payload,
    build_person_payload,
    canonicalize_domain,
    compute_idempotency_key,
    sanitize_and_partition_emails,
)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class TwentyApiError(RuntimeError):
    """Raised when Twenty CRM returns a non-retryable HTTP error or exhausts retries."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Twenty API HTTP {status_code}: {message}")
        self.status_code = status_code


def _extract_records_list(payload: Any, collection_key: str) -> list[dict[str, Any]]:
    """
    Unwrap Twenty CRM REST collection responses across both flat and Relay-style
    cursor envelopes:
      - `{"data": {"companies": [{"id": ...}]}}`
      - `{"data": {"companies": {"edges": [{"node": {"id": ...}}]}}}`
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return []
    col = data.get(collection_key)
    if isinstance(col, list):
        return [item for item in col if isinstance(item, dict)]
    if isinstance(col, dict):
        edges = col.get("edges")
        if isinstance(edges, list):
            out: list[dict[str, Any]] = []
            for edge in edges:
                if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
                    out.append(edge["node"])
            return out
    return []


def _extract_entity_id(payload: Any) -> str:
    """Extract entity UUID from Twenty REST create/update responses."""
    if isinstance(payload, dict):
        if isinstance(payload.get("id"), str):
            return payload["id"]
        data = payload.get("data")
        if isinstance(data, dict):
            if isinstance(data.get("id"), str):
                return data["id"]
            for val in data.values():
                if isinstance(val, dict) and isinstance(val.get("id"), str):
                    return val["id"]
    raise TwentyApiError(500, f"Unable to locate entity `id` in response: {payload!r}")


class TwentyRestClient:
    """Async REST client for Twenty CRM (`/rest/companies`, `/rest/people`, etc.)."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.twenty.com",
        api_key: str,
        timeout: float = 20.0,
        max_retries: int = 4,
        base_backoff_seconds: float = 0.5,
        sleep_fn: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_retries = max_retries
        self.base_backoff_seconds = base_backoff_seconds
        self._sleep = sleep_fn
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "twenty-crm-apify-b2b-sync/1.0.0",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> TwentyRestClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.request(method, path, params=params, json=json_body)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise TwentyApiError(503, f"Transport error after {attempt + 1} attempts: {exc}") from exc
                await self._sleep(self.base_backoff_seconds * (2**attempt))
                continue

            if resp.status_code in RETRYABLE_STATUS_CODES:
                if attempt >= self.max_retries:
                    raise TwentyApiError(resp.status_code, resp.text[:400])
                retry_after = resp.headers.get("Retry-After")
                delay = self.base_backoff_seconds * (2**attempt)
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        pass
                await self._sleep(delay)
                continue

            if resp.status_code >= 400:
                raise TwentyApiError(resp.status_code, resp.text[:500])

            if not resp.text.strip():
                return {}
            return resp.json()

        raise TwentyApiError(503, f"Request failed: {last_error}")

    async def find_company_by_domain(self, canonical_domain: str) -> dict[str, Any] | None:
        """
        Query `/rest/companies` using Twenty's composite field filter syntax
        `domainName.primaryLinkUrl[ilike]:%domain%` and verify exact canonical match.
        """
        filter_expr = f"domainName.primaryLinkUrl[ilike]:%{canonical_domain}%"
        payload = await self.request_json(
            "GET",
            "/rest/companies",
            params={"filter": filter_expr, "limit": 20},
        )
        for comp in _extract_records_list(payload, "companies"):
            dom_obj = comp.get("domainName") or {}
            raw_link = dom_obj.get("primaryLinkUrl") if isinstance(dom_obj, dict) else ""
            if raw_link:
                try:
                    if canonicalize_domain(raw_link) == canonical_domain:
                        return comp
                except ValueError:
                    continue
        return None

    async def upsert_company(
        self, canonical_domain: str, payload: CompanyUpsertPayload
    ) -> tuple[str, bool]:
        """Return `(company_id, created_new)`."""
        existing = await self.find_company_by_domain(canonical_domain)
        body = payload.to_twenty_json()
        if existing and isinstance(existing.get("id"), str):
            comp_id = existing["id"]
            await self.request_json("PATCH", f"/rest/companies/{comp_id}", json_body=body)
            return comp_id, False

        created = await self.request_json("POST", "/rest/companies", json_body=body)
        return _extract_entity_id(created), True

    async def find_person_by_email(self, primary_email: str) -> dict[str, Any] | None:
        filter_expr = f"emails.primaryEmail[eq]:{primary_email}"
        payload = await self.request_json(
            "GET",
            "/rest/people",
            params={"filter": filter_expr, "limit": 10},
        )
        for person in _extract_records_list(payload, "people"):
            emails_obj = person.get("emails") or {}
            existing_primary = (
                emails_obj.get("primaryEmail", "").strip().lower()
                if isinstance(emails_obj, dict)
                else ""
            )
            if existing_primary == primary_email.lower():
                return person
        return None

    async def upsert_person(self, payload: PersonUpsertPayload) -> tuple[str, bool]:
        """Return `(person_id, created_new)`."""
        existing = await self.find_person_by_email(payload.emails.primaryEmail)
        body = payload.to_twenty_json()
        if existing and isinstance(existing.get("id"), str):
            person_id = existing["id"]
            await self.request_json("PATCH", f"/rest/people/{person_id}", json_body=body)
            return person_id, False

        created = await self.request_json("POST", "/rest/people", json_body=body)
        return _extract_entity_id(created), True

    async def create_company_note(
        self,
        *,
        company_id: str,
        person_id: str | None,
        note_payload: NoteCreatePayload,
    ) -> str:
        created_note = await self.request_json(
            "POST",
            "/rest/notes",
            json_body=note_payload.to_twenty_json(),
        )
        note_id = _extract_entity_id(created_note)
        target_payload = NoteTargetCreatePayload(
            noteId=note_id,
            targetCompanyId=company_id,
            targetPersonId=person_id,
        )
        await self.request_json(
            "POST",
            "/rest/noteTargets",
            json_body=target_payload.to_twenty_json(),
        )
        return note_id

    async def create_opportunity(self, payload: OpportunityCreatePayload) -> str:
        created = await self.request_json(
            "POST",
            "/rest/opportunities",
            json_body=payload.to_twenty_json(),
        )
        return _extract_entity_id(created)


# ---------------------------------------------------------------------------
# Stateful In-Memory Mock Transport (`--demo-mock` & Unit Tests)
# ---------------------------------------------------------------------------


class InMemoryTwentyMockTransport(httpx.AsyncBaseTransport):
    """
    Simulates Twenty CRM's exact `/rest/*` endpoints in memory, including
    Relay-style `edges/node` pagination envelopes and optional transient 429/503
    injection for resilience testing.
    """

    def __init__(self, *, fail_first_n_writes: int = 0, failure_status: int = 429) -> None:
        self.companies: dict[str, dict[str, Any]] = {}
        self.people: dict[str, dict[str, Any]] = {}
        self.notes: dict[str, dict[str, Any]] = {}
        self.note_targets: dict[str, dict[str, Any]] = {}
        self.opportunities: dict[str, dict[str, Any]] = {}
        self.request_log: list[tuple[str, str]] = []
        self._remaining_failures = fail_first_n_writes
        self._failure_status = failure_status

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        path = parsed.path
        query = parse_qs(parsed.query)
        method = request.method.upper()
        self.request_log.append((method, path))

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth.split(" ", 1)[1].strip()) < 3:
            return httpx.Response(401, json={"error": "Missing or invalid Bearer token"})

        if method in ("POST", "PATCH") and self._remaining_failures > 0:
            self._remaining_failures -= 1
            headers = {"Retry-After": "0.01"} if self._failure_status == 429 else {}
            return httpx.Response(
                self._failure_status,
                headers=headers,
                json={"error": f"Simulated HTTP {self._failure_status}"},
            )

        body = json.loads(request.content.decode("utf-8")) if request.content else {}

        # Companies
        if path == "/rest/companies" and method == "GET":
            flt = (query.get("filter") or [""])[0].lower()
            needle = flt.split("%")[1] if "%" in flt else ""
            edges = []
            for comp in self.companies.values():
                url_val = ((comp.get("domainName") or {}).get("primaryLinkUrl") or "").lower()
                if not needle or needle in url_val:
                    edges.append({"node": comp})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "companies": {
                            "edges": edges,
                            "pageInfo": {"hasNextPage": False, "hasPreviousPage": False},
                        }
                    }
                },
            )

        if path == "/rest/companies" and method == "POST":
            cid = str(uuid.uuid4())
            rec = {"id": cid, **body}
            self.companies[cid] = rec
            return httpx.Response(201, json={"data": {"createCompany": rec}})

        if path.startswith("/rest/companies/") and method == "PATCH":
            cid = path.rsplit("/", 1)[-1]
            if cid not in self.companies:
                return httpx.Response(404, json={"error": "Company not found"})
            self.companies[cid].update(body)
            return httpx.Response(200, json={"data": {"updateCompany": self.companies[cid]}})

        # People
        if path == "/rest/people" and method == "GET":
            flt = (query.get("filter") or [""])[0]
            target_email = flt.split(":", 1)[-1].strip().lower() if ":" in flt else ""
            edges = []
            for person in self.people.values():
                p_email = ((person.get("emails") or {}).get("primaryEmail") or "").lower()
                if not target_email or p_email == target_email:
                    edges.append({"node": person})
            return httpx.Response(200, json={"data": {"people": {"edges": edges}}})

        if path == "/rest/people" and method == "POST":
            pid = str(uuid.uuid4())
            rec = {"id": pid, **body}
            self.people[pid] = rec
            return httpx.Response(201, json={"data": {"createPerson": rec}})

        if path.startswith("/rest/people/") and method == "PATCH":
            pid = path.rsplit("/", 1)[-1]
            if pid not in self.people:
                return httpx.Response(404, json={"error": "Person not found"})
            self.people[pid].update(body)
            return httpx.Response(200, json={"data": {"updatePerson": self.people[pid]}})

        # Notes & NoteTargets
        if path == "/rest/notes" and method == "POST":
            nid = str(uuid.uuid4())
            rec = {"id": nid, **body}
            self.notes[nid] = rec
            return httpx.Response(201, json={"data": {"createNote": rec}})

        if path == "/rest/noteTargets" and method == "POST":
            ntid = str(uuid.uuid4())
            rec = {"id": ntid, **body}
            self.note_targets[ntid] = rec
            return httpx.Response(201, json={"data": {"createNoteTarget": rec}})

        # Opportunities
        if path == "/rest/opportunities" and method == "POST":
            oid = str(uuid.uuid4())
            rec = {"id": oid, **body}
            self.opportunities[oid] = rec
            return httpx.Response(201, json={"data": {"createOpportunity": rec}})

        return httpx.Response(404, json={"error": f"Unhandled route {method} {path}"})


# ---------------------------------------------------------------------------
# High-Level Orchestrator: Merge Duplicate Domains + Sync Into Twenty CRM
# ---------------------------------------------------------------------------


def merge_leads_by_domain(
    raw_items: list[dict[str, Any]],
) -> tuple[list[tuple[str, ApifyEnrichedLead]], int]:
    """
    Validate raw Apify items via Pydantic v2 and merge multiple crawled URLs
    belonging to the same canonical domain (`acme.io/about` + `www.acme.io/contact`)
    into a single consolidated `ApifyEnrichedLead` record.
    Returns `(merged_domain_leads, skipped_errored_count)`.
    """
    grouped: dict[str, ApifyEnrichedLead] = {}
    skipped_errors = 0

    for raw in raw_items:
        lead = ApifyEnrichedLead.model_validate(raw)
        if lead.error or (lead.status_code is not None and lead.status_code >= 400):
            skipped_errors += 1
            continue
        try:
            domain = canonicalize_domain(lead.url)
        except ValueError:
            skipped_errors += 1
            continue

        if domain not in grouped:
            grouped[domain] = lead
            continue

        existing = grouped[domain]
        merged_emails = list(dict.fromkeys([*existing.emails, *lead.emails]))
        merged_phones = list(dict.fromkeys([*existing.phones, *lead.phones]))
        merged_tech = list(dict.fromkeys([*existing.tech_stack, *lead.tech_stack]))
        merged_ld = list(dict.fromkeys([*existing.json_ld_types, *lead.json_ld_types]))

        social_dict = existing.social_profiles.model_dump()
        for k, v in lead.social_profiles.model_dump().items():
            if not social_dict.get(k) and v:
                social_dict[k] = v

        grouped[domain] = existing.model_copy(
            update={
                "company_name": existing.company_name or lead.company_name,
                "meta_description": existing.meta_description or lead.meta_description,
                "emails": merged_emails,
                "phones": merged_phones,
                "tech_stack": merged_tech,
                "json_ld_types": merged_ld,
                "social_profiles": ApifySocialProfiles.model_validate(social_dict),
                "pages_crawled": existing.pages_crawled + lead.pages_crawled,
                "city": existing.city or lead.city,
                "country": existing.country or lead.country,
                "estimated_deal_usd": existing.estimated_deal_usd
                if existing.estimated_deal_usd is not None
                else lead.estimated_deal_usd,
            }
        )

    return list(grouped.items()), skipped_errors


class TwentySyncEngine:
    """End-to-end idempotent synchronization workflow from Apify leads to Twenty CRM."""

    def __init__(
        self,
        *,
        client: TwentyRestClient | None,
        ledger: SyncAuditLedger,
        dry_run: bool = False,
        create_notes: bool = True,
        create_opportunities: bool = False,
        opportunity_stage: str = "NEW",
        currency_code: str = "USD",
    ) -> None:
        self.client = client
        self.ledger = ledger
        self.dry_run = dry_run
        self.create_notes = create_notes
        self.create_opportunities = create_opportunities
        self.opportunity_stage = opportunity_stage
        self.currency_code = currency_code

    async def sync_items(
        self, raw_items: list[dict[str, Any]], *, mode_label: str = "live"
    ) -> tuple[SyncRunSummary, list[dict[str, Any]]]:
        merged_leads, skipped_errors = merge_leads_by_domain(raw_items)
        summary = SyncRunSummary(
            mode="dry-run" if self.dry_run else mode_label,
            input_records=len(raw_items),
            unique_domains=len(merged_leads),
            skipped_errored_leads=skipped_errors,
        )
        preview_payloads: list[dict[str, Any]] = []

        for domain, lead in merged_leads:
            valid_emails, rejected_cnt = sanitize_and_partition_emails(lead.emails)
            summary.rejected_emails += rejected_cnt

            idem_key = compute_idempotency_key(domain, valid_emails, lead.tech_stack)
            if self.ledger.has_key(idem_key):
                summary.skipped_ledger_duplicates += 1
                continue

            comp_payload = build_company_payload(lead, domain)
            note_payload = build_enrichment_note_payload(lead, domain)

            if self.dry_run:
                placeholder_company_id = f"dryrun-company-{domain}"
                person_payload = build_person_payload(
                    lead, placeholder_company_id, comp_payload.name, valid_emails
                )
                opp_payload = (
                    build_opportunity_payload(
                        comp_payload.name,
                        placeholder_company_id,
                        "dryrun-person" if person_payload else None,
                        stage=self.opportunity_stage,
                        estimated_deal_usd=lead.estimated_deal_usd,
                        currency_code=self.currency_code,
                    )
                    if self.create_opportunities
                    else None
                )
                preview_payloads.append(
                    {
                        "canonical_domain": domain,
                        "idempotency_key": idem_key,
                        "company": comp_payload.to_twenty_json(),
                        "person": person_payload.to_twenty_json() if person_payload else None,
                        "note": note_payload.to_twenty_json() if self.create_notes else None,
                        "opportunity": opp_payload.to_twenty_json() if opp_payload else None,
                    }
                )
                continue

            if self.client is None:
                raise RuntimeError("TwentyRestClient is required when dry_run=False.")

            company_id, comp_created = await self.client.upsert_company(domain, comp_payload)
            if comp_created:
                summary.companies_created += 1
            else:
                summary.companies_updated += 1

            person_id: str | None = None
            person_payload = build_person_payload(
                lead, company_id, comp_payload.name, valid_emails
            )
            if person_payload is not None:
                person_id, person_created = await self.client.upsert_person(person_payload)
                if person_created:
                    summary.people_created += 1
                else:
                    summary.people_updated += 1

            note_id: str | None = None
            if self.create_notes:
                note_id = await self.client.create_company_note(
                    company_id=company_id,
                    person_id=person_id,
                    note_payload=note_payload,
                )
                summary.notes_attached += 1

            opp_id: str | None = None
            if self.create_opportunities:
                opp_payload = build_opportunity_payload(
                    comp_payload.name,
                    company_id,
                    person_id,
                    stage=self.opportunity_stage,
                    estimated_deal_usd=lead.estimated_deal_usd,
                    currency_code=self.currency_code,
                )
                opp_id = await self.client.create_opportunity(opp_payload)
                summary.opportunities_created += 1

            record = self.ledger.record(
                idempotency_key=idem_key,
                canonical_domain=domain,
                company_id=company_id,
                person_id=person_id,
                note_id=note_id,
                opportunity_id=opp_id,
                action="created" if comp_created else "updated",
            )
            preview_payloads.append(record)

        return summary, preview_payloads
