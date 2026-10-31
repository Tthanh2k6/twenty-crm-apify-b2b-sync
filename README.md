# twenty-crm-apify-b2b-sync

Production-grade asynchronous Python 3.11+ synchronization engine that pipes enriched B2B company and contact datasets from the public Apify Store Actor [`thanh81646/apify-b2b-lead-and-contact-enricher`](https://apify.com/thanh81646/apify-b2b-lead-and-contact-enricher) directly into **Twenty CRM** (Cloud or Self-Hosted) via Twenty's REST API.

Built by **Trung Thanh Tran** ([Portfolio](https://trungthanhtran.vercel.app) | [Apify Store](https://apify.com/thanh81646) | [GitHub](https://github.com/Tthanh2k6)).

---

## Architecture Overview

```mermaid
flowchart LR
    A["Apify Store Actor (apify-b2b-lead-and-contact-enricher)"] -->|"ACTOR.RUN.SUCCEEDED Webhook / Dataset JSON"| B["Normalizer & Hygiene Layer (Pydantic v2)"]
    B -->|"Canonical Domain Consolidation"| C["SHA-256 JSONL Audit Ledger"]
    C -->|"New or Updated Fingerprint"| D["Async TwentyRestClient (Bearer Auth + 429 Retry)"]
    D -->|"GET + POST/PATCH /rest/companies"| E["Twenty CRM: Company"]
    D -->|"GET + POST/PATCH /rest/people"| F["Twenty CRM: Person (linked via companyId)"]
    D -->|"POST /rest/notes + /rest/noteTargets"| G["Twenty CRM: Tech Stack Markdown Note"]
    D -->|"Optional POST /rest/opportunities"| H["Twenty CRM: NEW Stage Opportunity"]
```

---

## Key Engineering Capabilities

1. **Twenty CRM 2026 Composite Schema Fidelity:** Maps flat scraper records into Twenty's exact nested composite metadata structures (`LinksMetadata`, `EmailsMetadata`, `PhonesMetadata`, `FullNameMetadata`, `AddressMetadata`, `CurrencyMetadata` in micros, and `RichTextMetadata` `bodyV2.markdown`) defined in `twentyhq/twenty`.
2. **Multi-Page Domain Consolidation & Upsert Deduplication:** Crawling both `https://www.nordicflow-analytics.io/` and `https://nordicflow-analytics.io/contact` consolidates emails, phones, social links, and tech-stack tags under the single canonical domain `nordicflow-analytics.io` and queries `/rest/companies?filter=domainName.primaryLinkUrl[ilike]:%...%` before writing so duplicate `Company` rows are never created.
3. **B2B Email Hygiene & Contact Ranking:** Rejects telemetry DSNs (`sentry.io`, `wixpress.com`), image asset false positives (`banner@2x.png`), placeholder domains (`example.com`), and `noreply@` addresses while promoting named human inboxes (`maja.lindqvist@`) ahead of generic role aliases (`hello@`, `info@`) for `emails.primaryEmail`.
4. **Resilient Async HTTP Transport:** Uses `httpx.AsyncClient` with `Retry-After` header parsing and exponential backoff across `HTTP 429`, `500`, `502`, `503`, and `504` responses.
5. **Idempotent Append-Only Audit Ledger:** Persists deterministic SHA-256 content hashes in `sync_audit.jsonl` so repeated Apify webhook deliveries or re-runs skip unchanged leads in `O(1)`.
6. **HMAC-Verified Apify Webhook Handler:** Includes `twenty_sync.webhook` with constant-time `HMAC-SHA256` signature verification for automated `ACTOR.RUN.SUCCEEDED` ingestion.

---

## Field Mapping (Apify Actor -> Twenty CRM Standard Objects)

| Source Field (`apify-b2b-lead-and-contact-enricher`) | Target Twenty Object | Target Twenty Field & Composite Structure |
| :--- | :--- | :--- |
| `url` (canonicalized host) | `Company` | `domainName: { primaryLinkUrl, primaryLinkLabel, secondaryLinks }` |
| `company_name` (SEO-cleaned) | `Company` | `name` (`string`) |
| `social_profiles.linkedin` (`/company/`) | `Company` | `linkedinLink: { primaryLinkUrl, primaryLinkLabel }` |
| `social_profiles` (`twitter_x`, `youtube`, etc.) | `Company` | `domainName.secondaryLinks[]` (`{ url, label }`) |
| `city`, `country` | `Company` | `address: { addressCity, addressCountry }` |
| `emails` (cleaned & priority-ranked) | `Person` | `emails: { primaryEmail, additionalEmails }` + `name: { firstName, lastName }` |
| `phones` (E.164 & national parser) | `Person` | `phones: { primaryPhoneNumber, primaryPhoneCallingCode, primaryPhoneCountryCode, additionalPhones }` |
| Resolved `Company.id` | `Person` | `companyId` (`UUID` foreign key) |
| `tech_stack`, `json_ld_types`, `meta_description` | `Note` + `NoteTarget` | `title` + `bodyV2: { markdown }` linked via `targetCompanyId` & `targetPersonId` |
| `estimated_deal_usd` (optional) | `Opportunity` | `stage: "NEW"`, `amount: { amountMicros, currencyCode }`, `companyId`, `pointOfContactId` |

---

## Quickstart

### 1. Install

```bash
pip install -e ".[dev]"
```

### 2. Offline Dry-Run (Inspect Exact Twenty Payloads)

```bash
python -m twenty_sync.cli --dry-run --create-opportunities --output-preview ./preview_payloads.json
```

### 3. Stateful End-to-End Mock Verification (`--demo-mock`)

Executes the full HTTP lookup, upsert, Note attachment, Opportunity creation, and JSONL ledger pipeline against an in-memory Twenty CRM `/rest/*` server:

```bash
python -m twenty_sync.cli --demo-mock --create-opportunities --ledger ./demo_sync_audit.jsonl
```

### 4. Live Sync to Twenty Cloud or Self-Hosted Instance

Generate an API key in your Twenty workspace under **Settings -> Developers -> API**, then run:

```bash
export TWENTY_BASE_URL="https://api.twenty.com"
export TWENTY_API_KEY="your_twenty_bearer_token"

# Sync from a local Apify export file
python -m twenty_sync.cli --input ./data/sample_apify_b2b_leads.json --create-opportunities --ledger ./sync_audit.jsonl

# Or pull directly from a completed Apify Actor Dataset ID
python -m twenty_sync.cli --apify-dataset-id "your_apify_dataset_id" --ledger ./sync_audit.jsonl
```

---

## Configuring Automated Apify Webhooks

In the Apify Console for [`thanh81646/apify-b2b-lead-and-contact-enricher`](https://apify.com/thanh81646/apify-b2b-lead-and-contact-enricher):

1. Open **Integrations -> Webhooks -> Create new webhook**.
2. Select event **`Run succeeded` (`ACTOR.RUN.SUCCEEDED`)**.
3. Sign requests with `APIFY_WEBHOOK_SECRET` in `X-Apify-Webhook-Signature` and pass the incoming payload to `verify_apify_webhook_signature()` + `handle_apify_webhook_event()` in `src/twenty_sync/webhook.py`.

---

## Running the Test Suite

```bash
pytest -v
```

Or with Python's standard library test runner:

```bash
python -m unittest discover -s tests -v
```

---

## License

MIT License. Copyright (c) 2026 Trung Thanh Tran.
