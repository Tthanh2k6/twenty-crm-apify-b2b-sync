"""
Comprehensive unit and integration test suite for `twenty-crm-apify-b2b-sync`.
Compatible with both `pytest` and `python -m unittest`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from twenty_sync.cli import main as cli_main
from twenty_sync.client import (
    InMemoryTwentyMockTransport,
    TwentyApiError,
    TwentyRestClient,
    TwentySyncEngine,
    merge_leads_by_domain,
)
from twenty_sync.ledger import SyncAuditLedger
from twenty_sync.models import ApifyEnrichedLead
from twenty_sync.normalizer import (
    build_company_payload,
    build_opportunity_payload,
    build_person_payload,
    canonicalize_domain,
    clean_company_name,
    parse_phones_metadata,
    sanitize_and_partition_emails,
)
from twenty_sync.webhook import handle_apify_webhook_event, verify_apify_webhook_signature


class TestTwentyCrmApifySync(unittest.TestCase):
    def test_canonicalize_domain_strips_scheme_www_port_and_path(self) -> None:
        self.assertEqual(
            canonicalize_domain("https://WWW.NordicFlow-Analytics.io:8443/contact?ref=apify"),
            "nordicflow-analytics.io",
        )
        self.assertEqual(canonicalize_domain("veridian-biopack.sg/about"), "veridian-biopack.sg")
        with self.assertRaises(ValueError):
            canonicalize_domain("   ")

    def test_clean_company_name_strips_seo_boilerplate(self) -> None:
        self.assertEqual(
            clean_company_name("Home | Veridian BioPack Singapore", "veridian-biopack.sg"),
            "Veridian BioPack Singapore",
        )
        self.assertEqual(clean_company_name("", "acme-cloud.io"), "Acme Cloud")

    def test_email_hygiene_rejects_noise_and_prioritizes_human_contacts(self) -> None:
        raw = [
            "hello@nordicflow.io",
            "mailto:Maja.Lindqvist@NordicFlow.io?subject=Hi",
            "89af01@sentry.io",
            "team@sentry.wixpress.com",
            "banner@2x.png",
            "noreply@nordicflow.io",
            "user@example.com",
            "broken@@nordicflow.io",
            "maja.lindqvist@nordicflow.io",
        ]
        valid, rejected = sanitize_and_partition_emails(raw)
        self.assertEqual(valid, ["maja.lindqvist@nordicflow.io", "hello@nordicflow.io"])
        self.assertEqual(rejected, 6)

    def test_phone_metadata_parsing_e164_and_us_local(self) -> None:
        meta = parse_phones_metadata(["+84 90 123 4567", "(512) 640-8821", "123"])
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta.primaryPhoneCallingCode, "+84")
        self.assertEqual(meta.primaryPhoneCountryCode, "VN")
        self.assertEqual(meta.primaryPhoneNumber, "901234567")
        self.assertEqual(len(meta.additionalPhones), 1)
        self.assertEqual(meta.additionalPhones[0].countryCode, "US")

    def test_twenty_composite_payload_schema_alignment(self) -> None:
        lead = ApifyEnrichedLead.model_validate(
            {
                "url": "https://www.acme-ops.io/pricing",
                "company_name": "Acme Ops | Official Site",
                "emails": ["info@acme-ops.io", "alex.rivera@acme-ops.io"],
                "phones": ["+1 415-890-4312"],
                "social_profiles": {
                    "linkedin": "https://linkedin.com/company/acme-ops",
                    "twitter_x": "https://x.com/acmeops",
                },
                "tech_stack": ["Next.js", "Stripe"],
                "city": "San Francisco",
                "country": "United States",
                "estimated_deal_usd": 1500.0,
            }
        )
        comp = build_company_payload(lead, "acme-ops.io").to_twenty_json()
        self.assertEqual(comp["name"], "Acme Ops")
        self.assertEqual(comp["domainName"]["primaryLinkUrl"], "https://acme-ops.io")
        self.assertEqual(comp["domainName"]["primaryLinkLabel"], "acme-ops.io")
        self.assertEqual(comp["domainName"]["secondaryLinks"][0]["url"], "https://x.com/acmeops")
        self.assertEqual(
            comp["linkedinLink"]["primaryLinkUrl"], "https://linkedin.com/company/acme-ops"
        )
        self.assertEqual(comp["address"]["addressCity"], "San Francisco")

        valid_emails, _ = sanitize_and_partition_emails(lead.emails)
        person = build_person_payload(lead, "comp-uuid-1", "Acme Ops", valid_emails)
        self.assertIsNotNone(person)
        assert person is not None
        p_json = person.to_twenty_json()
        self.assertEqual(p_json["name"], {"firstName": "Alex", "lastName": "Rivera"})
        self.assertEqual(
            p_json["emails"],
            {"primaryEmail": "alex.rivera@acme-ops.io", "additionalEmails": ["info@acme-ops.io"]},
        )
        self.assertEqual(p_json["companyId"], "comp-uuid-1")

        opp = build_opportunity_payload(
            "Acme Ops", "comp-uuid-1", "person-uuid-1", estimated_deal_usd=1500.0
        ).to_twenty_json()
        self.assertEqual(opp["amount"], {"amountMicros": 1_500_000_000, "currencyCode": "USD"})
        self.assertEqual(opp["stage"], "NEW")

    def test_merge_leads_by_domain_consolidates_multi_page_crawls(self) -> None:
        raw = [
            {
                "url": "https://www.nordicflow.io",
                "company_name": "NordicFlow",
                "emails": ["hello@nordicflow.io"],
                "tech_stack": ["Next.js"],
                "status_code": 200,
            },
            {
                "url": "https://nordicflow.io/contact",
                "company_name": "Contact",
                "emails": ["cto@nordicflow.io", "hello@nordicflow.io"],
                "tech_stack": ["Stripe"],
                "status_code": 200,
            },
            {
                "url": "https://dead-site.io",
                "status_code": 502,
                "error": "Bad Gateway",
            },
        ]
        merged, skipped = merge_leads_by_domain(raw)
        self.assertEqual(skipped, 1)
        self.assertEqual(len(merged), 1)
        dom, lead = merged[0]
        self.assertEqual(dom, "nordicflow.io")
        self.assertEqual(lead.emails, ["hello@nordicflow.io", "cto@nordicflow.io"])
        self.assertEqual(set(lead.tech_stack), {"Next.js", "Stripe"})

    def test_retry_on_429_rate_limit_and_idempotent_company_patch(self) -> None:
        async def scenario() -> None:
            sleeps: list[float] = []

            async def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)

            mock_tx = InMemoryTwentyMockTransport(fail_first_n_writes=2, failure_status=429)
            async with TwentyRestClient(
                base_url="https://mock.twenty.local",
                api_key="valid_test_key",
                base_backoff_seconds=0.01,
                sleep_fn=fake_sleep,
                transport=mock_tx,
            ) as client:
                with tempfile.TemporaryDirectory() as tmp:
                    ledger_file = Path(tmp) / "audit.jsonl"
                    engine = TwentySyncEngine(
                        client=client,
                        ledger=SyncAuditLedger(ledger_file),
                        create_opportunities=True,
                    )
                    batch_1 = [
                        {
                            "url": "https://acme.io",
                            "company_name": "Acme Corp",
                            "emails": ["jane.doe@acme.io"],
                            "tech_stack": ["React"],
                            "estimated_deal_usd": 1000,
                        }
                    ]
                    summary_1, _ = await engine.sync_items(batch_1)
                    self.assertEqual(summary_1.companies_created, 1)
                    self.assertEqual(summary_1.people_created, 1)
                    self.assertEqual(summary_1.opportunities_created, 1)
                    self.assertEqual(len(sleeps), 2)

                    # Re-running identical payload hits the JSONL idempotency ledger (0 writes)
                    engine_repeat = TwentySyncEngine(
                        client=client,
                        ledger=SyncAuditLedger(ledger_file),
                    )
                    summary_repeat, _ = await engine_repeat.sync_items(batch_1)
                    self.assertEqual(summary_repeat.skipped_ledger_duplicates, 1)
                    self.assertEqual(summary_repeat.companies_created, 0)

                    # Enriched update for same domain updates existing Company & Person via PATCH
                    batch_2 = [
                        {
                            "url": "https://www.acme.io/team",
                            "company_name": "Acme Corporation",
                            "emails": ["jane.doe@acme.io"],
                            "tech_stack": ["React", "HubSpot"],
                        }
                    ]
                    summary_2, _ = await engine_repeat.sync_items(batch_2)
                    self.assertEqual(summary_2.companies_created, 0)
                    self.assertEqual(summary_2.companies_updated, 1)
                    self.assertEqual(summary_2.people_updated, 1)
                    self.assertEqual(len(mock_tx.companies), 1)
                    self.assertEqual(len(mock_tx.people), 1)

        asyncio.run(scenario())

    def test_unauthorized_api_key_raises_immediately_without_retries(self) -> None:
        async def scenario() -> None:
            mock_tx = InMemoryTwentyMockTransport()
            async with TwentyRestClient(
                base_url="https://mock.twenty.local",
                api_key="x",
                transport=mock_tx,
            ) as client:
                with self.assertRaises(TwentyApiError) as ctx:
                    await client.find_company_by_domain("acme.io")
                self.assertEqual(ctx.exception.status_code, 401)

        asyncio.run(scenario())

    def test_apify_webhook_hmac_and_dataset_ingestion(self) -> None:
        secret = "top_secret_webhook_key"
        event = {
            "eventType": "ACTOR.RUN.SUCCEEDED",
            "resource": {"defaultDatasetId": "ds_test_123"},
        }
        raw_body = json.dumps(event).encode("utf-8")
        digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

        self.assertTrue(verify_apify_webhook_signature(raw_body, f"sha256={digest}", secret))
        self.assertFalse(verify_apify_webhook_signature(raw_body, "sha256=deadbeef", secret))

        async def scenario() -> None:
            def apify_mock_handler(request: httpx.Request) -> httpx.Response:
                self.assertIn("/v2/datasets/ds_test_123/items", str(request.url))
                return httpx.Response(
                    200,
                    json=[
                        {
                            "url": "https://webhook-client.io",
                            "company_name": "Webhook Client Ltd",
                            "emails": ["founder@webhook-client.io"],
                            "tech_stack": ["Shopify"],
                        }
                    ],
                )

            twenty_mock = InMemoryTwentyMockTransport()
            async with TwentyRestClient(
                base_url="https://mock.twenty.local",
                api_key="valid_token",
                transport=twenty_mock,
            ) as client:
                engine = TwentySyncEngine(client=client, ledger=SyncAuditLedger(None))
                summary = await handle_apify_webhook_event(
                    event,
                    engine=engine,
                    apify_token="apify_tok",
                    apify_transport=httpx.MockTransport(apify_mock_handler),
                )
                self.assertEqual(summary.companies_created, 1)
                self.assertEqual(summary.people_created, 1)

        asyncio.run(scenario())

    def test_cli_dry_run_and_demo_mock_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            preview_file = Path(tmp) / "preview.json"
            ledger_file = Path(tmp) / "demo_audit.jsonl"

            rc_dry = cli_main(["--dry-run", "--output-preview", str(preview_file)])
            self.assertEqual(rc_dry, 0)
            preview_data = json.loads(preview_file.read_text(encoding="utf-8"))
            self.assertEqual(len(preview_data), 3)

            rc_demo = cli_main(
                ["--demo-mock", "--create-opportunities", "--ledger", str(ledger_file)]
            )
            self.assertEqual(rc_demo, 0)
            self.assertTrue(ledger_file.exists())
            lines = [ln for ln in ledger_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(lines), 3)


if __name__ == "__main__":
    unittest.main()
