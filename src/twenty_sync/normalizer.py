"""
Normalization, email hygiene, phone parsing, and composite payload builders
bridging Apify enrichment output to Twenty CRM standard objects.
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse

from .models import (
    AdditionalPhoneItem,
    AddressMetadata,
    ApifyEnrichedLead,
    CompanyUpsertPayload,
    CurrencyMetadata,
    EmailsMetadata,
    FullNameMetadata,
    LinksMetadata,
    NoteCreatePayload,
    OpportunityCreatePayload,
    PersonUpsertPayload,
    PhonesMetadata,
    RichTextV2Metadata,
    SecondaryLinkItem,
)

STRICT_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,15}$"
)

BLOCKED_EMAIL_DOMAINS = {
    "sentry.io",
    "sentry.wixpress.com",
    "sentry-next.wixpress.com",
    "wixpress.com",
    "wix.com",
    "example.com",
    "example.org",
    "domain.com",
    "email.com",
    "yoursite.com",
    "yourdomain.com",
    "company.com",
    "test.com",
    "localhost",
}

BLOCKED_ASSET_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".css",
    ".js",
    ".woff",
    ".woff2",
    ".ttf",
    ".map",
)

ROLE_INBOX_PREFIXES = {
    "info",
    "hello",
    "contact",
    "support",
    "help",
    "sales",
    "team",
    "office",
    "admin",
    "enquiries",
    "inquiries",
    "billing",
    "careers",
    "jobs",
    "press",
    "media",
    "marketing",
    "partnerships",
    "noreply",
    "no-reply",
}

CALLING_CODE_TO_ISO2: list[tuple[str, str]] = [
    ("+84", "VN"),
    ("+65", "SG"),
    ("+61", "AU"),
    ("+49", "DE"),
    ("+44", "GB"),
    ("+33", "FR"),
    ("+31", "NL"),
    ("+81", "JP"),
    ("+971", "AE"),
    ("+1", "US"),
]

TITLE_SEPARATOR_RE = re.compile(r"\s*(?:\||-|\u2013|\u2014|:)\s*")


def canonicalize_domain(raw_url: str) -> str:
    """
    Extract a deterministic lowercase bare domain from any URL or domain string.
    Example: 'https://www.AcmeAnalytics.io:8443/contact?ref=1' -> 'acmeanalytics.io'
    """
    candidate = (raw_url or "").strip()
    if not candidate:
        raise ValueError("Empty URL cannot be canonicalized.")
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host or " " in host:
        raise ValueError(f"Invalid hostname extracted from {raw_url!r}")
    return host


def clean_company_name(raw_name: str, canonical_domain: str) -> str:
    """Strip SEO title noise or derive a clean fallback company name from the domain."""
    cleaned = re.sub(r"\s+", " ", (raw_name or "")).strip()
    if cleaned:
        parts = [p.strip() for p in TITLE_SEPARATOR_RE.split(cleaned) if p.strip()]
        noise_phrases = (
            "home",
            "welcome",
            "official site",
            "all products",
            "online store",
            "homepage",
        )
        filtered = [p for p in parts if p.lower() not in noise_phrases]
        chosen = filtered[0] if filtered else parts[0]
        if 2 <= len(chosen) <= 90:
            return chosen

    stem = canonical_domain.split(".")[0]
    words = [w.capitalize() for w in re.split(r"[-_]+", stem) if w]
    return " ".join(words) if words else canonical_domain


def sanitize_and_partition_emails(raw_emails: list[str]) -> tuple[list[str], int]:
    """
    Validate, deduplicate, and rank business emails so human contacts appear
    before role-based aliases (`info@`, `hello@`), while rejecting malformed
    addresses, `noreply@`, Sentry DSNs, and asset filenames (`logo@2x.png`).
    """
    seen: set[str] = set()
    personal: list[str] = []
    role_based: list[str] = []
    rejected = 0

    for item in raw_emails or []:
        candidate = (item or "").strip()
        if candidate.lower().startswith("mailto:"):
            candidate = candidate[7:].split("?", 1)[0].strip()
        candidate = candidate.strip(".,;:()[]<>\"'").lower()
        if not candidate:
            continue

        if any(candidate.endswith(ext) for ext in BLOCKED_ASSET_SUFFIXES):
            rejected += 1
            continue
        if not STRICT_EMAIL_RE.match(candidate):
            rejected += 1
            continue

        local_part, domain_part = candidate.rsplit("@", 1)
        if domain_part in BLOCKED_EMAIL_DOMAINS or domain_part.endswith(".local"):
            rejected += 1
            continue
        if local_part in ("noreply", "no-reply", "donotreply", "do-not-reply"):
            rejected += 1
            continue
        if re.search(r"@\d+x$", local_part):
            rejected += 1
            continue

        if candidate in seen:
            continue
        seen.add(candidate)

        prefix_root = re.split(r"[.+_-]", local_part)[0]
        if local_part in ROLE_INBOX_PREFIXES or prefix_root in ROLE_INBOX_PREFIXES:
            role_based.append(candidate)
        else:
            personal.append(candidate)

    return personal + role_based, rejected


def parse_phones_metadata(raw_phones: list[str]) -> PhonesMetadata | None:
    """Convert raw scraped phone strings into Twenty CRM `PhonesMetadata`."""
    parsed_items: list[tuple[str, str, str]] = []
    seen_digits: set[str] = set()

    for raw in raw_phones or []:
        cleaned = (raw or "").strip()
        if not cleaned:
            continue
        has_plus = cleaned.startswith("+")
        digits = re.sub(r"\D", "", cleaned)
        if len(digits) < 7 or len(digits) > 15:
            continue
        if digits in seen_digits:
            continue
        seen_digits.add(digits)

        calling_code = ""
        country_code = ""
        national_number = digits

        if has_plus:
            e164 = f"+{digits}"
            for prefix, iso2 in CALLING_CODE_TO_ISO2:
                if e164.startswith(prefix) and len(digits) > len(prefix) - 1 + 5:
                    calling_code = prefix
                    country_code = iso2
                    national_number = digits[len(prefix) - 1 :]
                    break
            if not calling_code:
                calling_code = f"+{digits[:1]}"
                national_number = digits[1:]
        elif len(digits) == 10:
            calling_code = "+1"
            country_code = "US"
            national_number = digits

        parsed_items.append((national_number, calling_code, country_code))

    if not parsed_items:
        return None

    primary_num, primary_cc, primary_iso = parsed_items[0]
    additional = [
        AdditionalPhoneItem(number=num, callingCode=cc, countryCode=iso)
        for num, cc, iso in parsed_items[1:]
    ]
    return PhonesMetadata(
        primaryPhoneNumber=primary_num,
        primaryPhoneCallingCode=primary_cc,
        primaryPhoneCountryCode=primary_iso,
        additionalPhones=additional,
    )


def derive_contact_name_and_role(primary_email: str, company_name: str) -> tuple[FullNameMetadata, str]:
    """Infer a sensible `FullNameMetadata` and `jobTitle` from the primary email."""
    local_part = primary_email.split("@", 1)[0].lower()
    local_clean = local_part.split("+", 1)[0]

    if local_clean in ROLE_INBOX_PREFIXES:
        label = local_clean.capitalize()
        return FullNameMetadata(firstName=label, lastName="Inbox"), f"{label} Contact ({company_name})"

    tokens = [t for t in re.split(r"[._-]+", local_clean) if t and not t.isdigit()]
    if len(tokens) >= 2:
        return (
            FullNameMetadata(firstName=tokens[0].capitalize(), lastName=" ".join(t.capitalize() for t in tokens[1:3])),
            "Enriched Contact",
        )
    if len(tokens) == 1 and len(tokens[0]) >= 2:
        return FullNameMetadata(firstName=tokens[0].capitalize(), lastName=""), "Enriched Contact"
    return FullNameMetadata(firstName="Primary", lastName="Contact"), "Enriched Contact"


def normalize_http_url(raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    val = raw_url.strip()
    if not val:
        return None
    if not val.startswith(("http://", "https://")):
        val = f"https://{val}"
    parsed = urlparse(val)
    if not parsed.hostname or "." not in parsed.hostname:
        return None
    return val


def build_company_payload(lead: ApifyEnrichedLead, canonical_domain: str) -> CompanyUpsertPayload:
    company_name = clean_company_name(lead.company_name, canonical_domain)

    secondary_links: list[SecondaryLinkItem] = []
    social_map = [
        ("X / Twitter", lead.social_profiles.twitter_x),
        ("Facebook", lead.social_profiles.facebook),
        ("Instagram", lead.social_profiles.instagram),
        ("YouTube", lead.social_profiles.youtube),
        ("TikTok", lead.social_profiles.tiktok),
    ]
    for label, link in social_map:
        valid_link = normalize_http_url(link)
        if valid_link:
            secondary_links.append(SecondaryLinkItem(url=valid_link, label=label))

    linkedin_url = normalize_http_url(lead.social_profiles.linkedin)
    linkedin_meta = (
        LinksMetadata(primaryLinkUrl=linkedin_url, primaryLinkLabel="LinkedIn")
        if linkedin_url and "/company/" in linkedin_url.lower()
        else None
    )

    address_meta = None
    if lead.city or lead.country:
        address_meta = AddressMetadata(
            addressCity=(lead.city or "").strip(),
            addressCountry=(lead.country or "").strip(),
        )

    return CompanyUpsertPayload(
        name=company_name,
        domainName=LinksMetadata(
            primaryLinkUrl=f"https://{canonical_domain}",
            primaryLinkLabel=canonical_domain,
            secondaryLinks=secondary_links,
        ),
        linkedinLink=linkedin_meta,
        address=address_meta,
    )


def build_person_payload(
    lead: ApifyEnrichedLead,
    company_id: str,
    company_name: str,
    valid_emails: list[str],
) -> PersonUpsertPayload | None:
    if not valid_emails:
        return None
    primary_email = valid_emails[0]
    additional_emails = valid_emails[1:]
    full_name, job_title = derive_contact_name_and_role(primary_email, company_name)
    phones_meta = parse_phones_metadata(lead.phones)

    linkedin_url = normalize_http_url(lead.social_profiles.linkedin)
    person_linkedin = (
        LinksMetadata(primaryLinkUrl=linkedin_url, primaryLinkLabel="LinkedIn")
        if linkedin_url and "/in/" in linkedin_url.lower()
        else None
    )

    return PersonUpsertPayload(
        name=full_name,
        emails=EmailsMetadata(
            primaryEmail=primary_email,
            additionalEmails=additional_emails,
        ),
        phones=phones_meta,
        linkedinLink=person_linkedin,
        jobTitle=job_title,
        companyId=company_id,
    )


def build_enrichment_note_payload(lead: ApifyEnrichedLead, canonical_domain: str) -> NoteCreatePayload:
    lines = [
        f"### Automated Apify Enrichment (`{canonical_domain}`)",
        f"- **Source URL:** https://{canonical_domain}",
        f"- **HTTP Status / Pages Crawled:** `{lead.status_code or 200}` ({lead.pages_crawled} page(s))",
    ]
    if lead.tech_stack:
        lines.append(f"- **Detected Tech Stack:** {', '.join(sorted(set(lead.tech_stack)))}")
    if lead.json_ld_types:
        lines.append(f"- **Schema.org JSON-LD Entities:** {', '.join(sorted(set(lead.json_ld_types)))}")
    if lead.meta_description:
        desc = re.sub(r"\s+", " ", lead.meta_description).strip()[:280]
        lines.append(f"- **Meta Summary:** {desc}")

    return NoteCreatePayload(
        title=f"Apify Tech Stack & Lead Profile ({canonical_domain})",
        bodyV2=RichTextV2Metadata(markdown="\n".join(lines)),
    )


def build_opportunity_payload(
    company_name: str,
    company_id: str,
    point_of_contact_id: str | None,
    stage: str = "NEW",
    estimated_deal_usd: float | None = None,
    currency_code: str = "USD",
) -> OpportunityCreatePayload:
    amount_meta = None
    if estimated_deal_usd is not None and estimated_deal_usd >= 0:
        amount_meta = CurrencyMetadata(
            amountMicros=int(round(estimated_deal_usd * 1_000_000)),
            currencyCode=currency_code,
        )
    return OpportunityCreatePayload(
        name=f"{company_name} - Outbound Enrichment Lead",
        stage=stage,
        amount=amount_meta,
        companyId=company_id,
        pointOfContactId=point_of_contact_id,
    )


def compute_idempotency_key(
    canonical_domain: str,
    valid_emails: list[str],
    tech_stack: list[str],
) -> str:
    canonical_blob = json.dumps(
        {
            "domain": canonical_domain,
            "emails": sorted(valid_emails),
            "tech_stack": sorted(set(tech_stack)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_blob.encode("utf-8")).hexdigest()
