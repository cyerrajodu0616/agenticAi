# Claude Code Prompt: PDF Location Resolver (product_registry + resolve_pdf_location tool)

Paste this into Claude Code inside the `agenticAi` repo, after the MVP scaffold from
`langgraph-personal-assistant-mvp-prompt.md` has been applied (this prompt extends
`assistant/db/schema.sql` and replaces the `assistant/tools/consent_lookup.py` stub).

---

## Context

We now have a real mapping that tells us, for a given application status and form number, which
storage folder a form's PDF lives in. Source of truth in production is Valkey, key
`CENTER<productId>CONTROL`, field `pdfA103Mapping` — same config pattern the arcCenter engine
already uses everywhere else (see `arcenter-engine` skill: `CENTER<productId>CONTROL` holds all
product-specific config, read via `getValueFromJsonKey(key, path)`).

IMPORTANT — corrected understanding, do not reintroduce the earlier (wrong) design: `pdfA103Mapping`
is a STATIC field name. It is spelled exactly that for every product, regardless of which product
it is. Only the `<productId>` in the Redis key prefix changes. There is NO "resolve product family
/ code name, then build a dynamic mapping key" step — `productId` comes directly from application
data (it's already a field there) and is plugged straight into `CENTER<productId>CONTROL.pdfA103Mapping`.
"A103" in the field name is a historical naming artifact, not a template placeholder.

What DOES vary per product is the CONTENT behind that static field name — different products have
different form numbers and structures. We currently have real content for exactly ONE productId:
`511801`. The other 4 productIds in this family — `314005`, `4127`, `614004`, `214005` — do NOT yet
have known mapping content. Do not assume they share 511801's content; treat each as needing its
own confirmed mapping before it can be resolved.

`dim_products` (MySQL, `arcCenter_portal` schema) is a SEPARATE, PARALLEL table — not part of the
PDF-path resolution logic at all. It has columns: `product_id, product_label, product_name,
under_construction, image_url, product_url, product_type, is_newbridge,
default_navbar_image_url, report_product_name, hubspot_product_name`. Its only use here is
enriching an answer with a human-readable product name (pick the field appropriate to the
audience — e.g. `product_label` or `hubspot_product_name` for a customer-facing answer,
`product_name` or `report_product_name` for an internal/audit answer) — it plays no role in
finding the PDF itself.

Two things are NOT yet available and must be built as clearly-marked stubs — do not fake their
behavior:
1. A restricted-access API endpoint exposing `CENTER<productId>CONTROL.pdfA103Mapping` — does not
   exist yet, is being built separately.
2. The exact contract (URL, auth, response shape) of the *existing generic* endpoint that returns
   an application's `productId` / `arcidStatus` / `state` for a given `arcId` — exists today, but
   its contract hasn't been provided. Build the client with a config-driven base URL and a clearly
   marked TODO for the real path/auth/response-field names.

`arcidStatus` is the confirmed field for application status (`decline`, `final`, `mibpend`,
`modifiedagedecline`, `npw_aif`, `npw_offerexpired_pending_payment`,
`npw_offerexpired_pending_producer_cert`, `npw_offerexpired_pending_signatures`,
`npw_payment_failure`, `npw_timeout`). It is not always reliable — the resolver must validate it
against the known set and flag (not silently proceed) when it doesn't match.

## Task 1 — Extend `assistant/db/schema.sql`

Append (don't replace existing tables) a local mirror of `arcCenter_portal.dim_products` — same
columns, not a redesigned schema. Column types below are best-effort (TEXT/BOOLEAN) since the real
DDL hasn't been confirmed — mark this explicitly and treat as needing verification against the
actual `dim_products` DDL before relying on type-sensitive queries:

```sql
-- TODO CONFIRM real column types against arcCenter_portal.dim_products DDL before trusting these.
CREATE TABLE IF NOT EXISTS dim_products_mirror (
    product_id TEXT PRIMARY KEY,
    product_label TEXT,
    product_name TEXT,
    under_construction BOOLEAN,
    image_url TEXT,
    product_url TEXT,
    product_type TEXT,
    is_newbridge BOOLEAN,
    default_navbar_image_url TEXT,
    report_product_name TEXT,
    hubspot_product_name TEXT,
    synced_at TIMESTAMPTZ DEFAULT now()
);
```

## Task 2 — Sync job `assistant/db/sync_dim_products.py`

A scheduled job (not a one-time seed) that connects to the `arcCenter_portal` MySQL schema,
reads all rows from `dim_products`, and upserts them into `dim_products_mirror` on `product_id`.

- Connection: `ARC_CENTER_PORTAL_MYSQL_URL` env var — TODO CONFIRM this is the right database to
  point at and that read access is appropriate to grant to this agent.
- This mirrors the WHOLE table as-is — do not pick a subset of columns or rename any of them,
  since which name field is used for which purpose (customer-facing vs. internal vs. reporting vs.
  CRM/HubSpot) is decided at answer-composition time, not at sync time.
- Intended to run on the same recurring cadence as the rest of the knowledge-base sync jobs (see
  `personal-assistant-plan.md` section 8b) — e.g. via the `schedule` skill, daily or weekly.
- On failure to connect, log clearly and leave the existing mirror data in place (stale-but-present
  beats empty) rather than wiping the table.

## Task 3 — `assistant/config/pdf_mappings/511801.json`

DEV/TEST FIXTURE ONLY (see Task 5 — production always fetches live from Valkey, never from this
file). Filename is the productId this content is confirmed for — do not name it `pdfA103Mapping.json`,
that field-name string is not the productId and naming the file that way invites exactly the
confusion we just corrected. Create this file with EXACTLY the following content (this is real
production reference data provided directly by the user, confirmed for productId 511801 specifically):

```json
{
  "decline": {
    "907": {"FormName": "Application for Whole Life Insurance", "ProviderFormNumber": "ICC24 CGA2000-24", "s3Location": "appsign"},
    "910": {"FormName": "eConsent form", "ProviderFormNumber": "CD-001-2410", "s3Location": "appsign"},
    "911": {"FormName": "Authorization to Use or Disclose Protected Health Information (HIPAA form)", "ProviderFormNumber": "UW-CD-001-2408", "s3Location": "appsign"},
    "912": {"FormName": "Information Practices Related to Underwriting Your Application (MIB pre-notice and FCRA disclosure)", "ProviderFormNumber": "UW-CD-002-2408", "s3Location": "consent"},
    "913": {"FormName": "GLBA Notice (Privacy Policy)", "ProviderFormNumber": "CGIC-GLBA (1221)", "s3Location": "consent"},
    "914": {"FormName": "Terminal Illness Accelerated Death Benefit Rider Summary and Disclosure Statement", "ProviderFormNumber": "ICC24 CGSD3002-24", "s3Location": "appsign"},
    "915": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-NAIC", "s3Location": "consent", "state": ["WI", "WA", "IL", "GA"]},
    "916": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-ME", "s3Location": "consent", "state": ["ME"]},
    "919": {"FormName": "AUD Letter", "ProviderFormNumber": "UW-COR-008-2410", "s3Location": "aud"},
    "932": {"FormName": "NAIC form", "ProviderFormNumber": "AFF_NAIC 8.25", "s3Location": "appsign", "state": ["AK","AL","AR","AZ","CO","CT","HI","IA","KY","LA","MD","ME","MO","MS","MT","NC","NE","NH","NJ","NM","OH","OR","RI","SC","SD","TX","UT","VA","VT","WI","WV"]}
  },
  "final": {
    "907": {"FormName": "Application for Whole Life Insurance", "ProviderFormNumber": "ICC24 CGA2000-24", "s3Location": "cip"},
    "910": {"FormName": "eConsent form", "ProviderFormNumber": "CD-001-2410", "s3Location": "appsign"},
    "911": {"FormName": "Authorization to Use or Disclose Protected Health Information (HIPAA form)", "ProviderFormNumber": "UW-CD-001-2408", "s3Location": "appsign"},
    "912": {"FormName": "Information Practices Related to Underwriting Your Application (MIB pre-notice and FCRA disclosure)", "ProviderFormNumber": "UW-CD-002-2408", "s3Location": "consent"},
    "913": {"FormName": "GLBA Notice (Privacy Policy)", "ProviderFormNumber": "CGIC-GLBA (1221)", "s3Location": "consent"},
    "914": {"FormName": "Terminal Illness Accelerated Death Benefit Rider Summary and Disclosure Statement", "ProviderFormNumber": "ICC24 CGSD3002-24", "s3Location": "cip"},
    "915": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-NAIC", "s3Location": "consent", "state": ["WI", "WA", "IL", "GA"]},
    "916": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-ME", "s3Location": "consent", "state": ["ME"]},
    "921": {"FormName": "Modified Endowment Contract Disclosure", "ProviderFormNumber": "LIFE-CD-001-2410", "s3Location": "cip"},
    "922": {"FormName": "Preliminary Statement of Costs", "ProviderFormNumber": "LIFE-CD-002-2410", "s3Location": "cip", "state": ["ME"]},
    "923": {"FormName": "PA Disclosure Statement", "ProviderFormNumber": "LIFE-CD-PA-001-2410", "s3Location": "appsign", "state": ["PA"]},
    "924": {"FormName": "eConsent form", "ProviderFormNumber": "CD-001-2410", "role": "otherPayor", "s3Location": "appsign"},
    "932": {"FormName": "NAIC form", "ProviderFormNumber": "AFF_NAIC 8.25", "s3Location": "cip", "state": ["AK","AL","AR","AZ","CO","CT","HI","IA","KY","LA","MD","ME","MO","MS","MT","NC","NE","NH","NJ","NM","OH","OR","RI","SC","SD","TX","UT","VA","VT","WI","WV"]}
  },
  "mibpend": {
    "907": {"FormName": "Application for Whole Life Insurance", "ProviderFormNumber": "ICC24 CGA2000-24", "s3Location": "appsign"},
    "910": {"FormName": "eConsent form", "ProviderFormNumber": "CD-001-2410", "s3Location": "appsign"},
    "911": {"FormName": "Authorization to Use or Disclose Protected Health Information (HIPAA form)", "ProviderFormNumber": "UW-CD-001-2408", "s3Location": "appsign"},
    "912": {"FormName": "Information Practices Related to Underwriting Your Application (MIB pre-notice and FCRA disclosure)", "ProviderFormNumber": "UW-CD-002-2408", "s3Location": "consent"},
    "913": {"FormName": "GLBA Notice (Privacy Policy)", "ProviderFormNumber": "CGIC-GLBA (1221)", "s3Location": "consent"},
    "914": {"FormName": "Terminal Illness Accelerated Death Benefit Rider Summary and Disclosure Statement", "ProviderFormNumber": "ICC24 CGSD3002-24", "s3Location": "appsign"},
    "915": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-NAIC", "s3Location": "consent", "state": ["WI", "WA", "IL", "GA"]},
    "916": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-ME", "s3Location": "consent", "state": ["ME"]},
    "920": {"FormName": "MIB Pend Letter", "ProviderFormNumber": "UW-COR-003-2410", "s3Location": "mib"},
    "932": {"FormName": "NAIC form", "ProviderFormNumber": "AFF_NAIC 8.25", "s3Location": "appsign", "state": ["AK","AL","AR","AZ","CO","CT","HI","IA","KY","LA","MD","ME","MO","MS","MT","NC","NE","NH","NJ","NM","OH","OR","RI","SC","SD","TX","UT","VA","VT","WI","WV"]}
  },
  "modifiedagedecline": {
    "907": {"FormName": "Application for Whole Life Insurance", "ProviderFormNumber": "ICC24 CGA2000-24", "s3Location": "appsign"},
    "910": {"FormName": "eConsent form", "ProviderFormNumber": "CD-001-2410", "s3Location": "appsign"},
    "911": {"FormName": "Authorization to Use or Disclose Protected Health Information (HIPAA form)", "ProviderFormNumber": "UW-CD-001-2408", "s3Location": "appsign"},
    "912": {"FormName": "Information Practices Related to Underwriting Your Application (MIB pre-notice and FCRA disclosure)", "ProviderFormNumber": "UW-CD-002-2408", "s3Location": "consent"},
    "913": {"FormName": "GLBA Notice (Privacy Policy)", "ProviderFormNumber": "CGIC-GLBA (1221)", "s3Location": "consent"},
    "914": {"FormName": "Terminal Illness Accelerated Death Benefit Rider Summary and Disclosure Statement", "ProviderFormNumber": "ICC24 CGSD3002-24", "s3Location": "appsign"},
    "915": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-NAIC", "s3Location": "consent", "state": ["WI", "WA", "IL", "GA"]},
    "916": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-ME", "s3Location": "consent", "state": ["ME"]},
    "932": {"FormName": "NAIC form", "ProviderFormNumber": "AFF_NAIC 8.25", "s3Location": "appsign", "state": ["AK","AL","AR","AZ","CO","CT","HI","IA","KY","LA","MD","ME","MO","MS","MT","NC","NE","NH","NJ","NM","OH","OR","RI","SC","SD","TX","UT","VA","VT","WI","WV"]}
  },
  "npw_aif": {
    "907": {"FormName": "Application for Whole Life Insurance", "ProviderFormNumber": "ICC24 CGA2000-24", "s3Location": "appsign"},
    "910": {"FormName": "eConsent form", "ProviderFormNumber": "CD-001-2410", "s3Location": "appsign"},
    "911": {"FormName": "Authorization to Use or Disclose Protected Health Information (HIPAA form)", "ProviderFormNumber": "UW-CD-001-2408", "s3Location": "appsign"},
    "912": {"FormName": "Information Practices Related to Underwriting Your Application (MIB pre-notice and FCRA disclosure)", "ProviderFormNumber": "UW-CD-002-2408", "s3Location": "consent"},
    "913": {"FormName": "GLBA Notice (Privacy Policy)", "ProviderFormNumber": "CGIC-GLBA (1221)", "s3Location": "consent"},
    "914": {"FormName": "Terminal Illness Accelerated Death Benefit Rider Summary and Disclosure Statement", "ProviderFormNumber": "ICC24 CGSD3002-24", "s3Location": "appsign"},
    "915": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-NAIC", "s3Location": "consent", "state": ["WI", "WA", "IL", "GA"]},
    "916": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-ME", "s3Location": "consent", "state": ["ME"]},
    "919": {"FormName": "AUD Letter", "ProviderFormNumber": "UW-COR-008-2410", "s3Location": "aud"},
    "932": {"FormName": "NAIC form", "ProviderFormNumber": "AFF_NAIC 8.25", "s3Location": "appsign", "state": ["AK","AL","AR","AZ","CO","CT","HI","IA","KY","LA","MD","ME","MO","MS","MT","NC","NE","NH","NJ","NM","OH","OR","RI","SC","SD","TX","UT","VA","VT","WI","WV"]}
  },
  "npw_offerexpired_pending_payment": {
    "907": {"FormName": "Application for Whole Life Insurance", "ProviderFormNumber": "ICC24 CGA2000-24", "s3Location": "appsign"},
    "910": {"FormName": "eConsent form", "ProviderFormNumber": "CD-001-2410", "s3Location": "appsign"},
    "911": {"FormName": "Authorization to Use or Disclose Protected Health Information (HIPAA form)", "ProviderFormNumber": "UW-CD-001-2408", "s3Location": "appsign"},
    "912": {"FormName": "Information Practices Related to Underwriting Your Application (MIB pre-notice and FCRA disclosure)", "ProviderFormNumber": "UW-CD-002-2408", "s3Location": "consent"},
    "913": {"FormName": "GLBA Notice (Privacy Policy)", "ProviderFormNumber": "CGIC-GLBA (1221)", "s3Location": "consent"},
    "914": {"FormName": "Terminal Illness Accelerated Death Benefit Rider Summary and Disclosure Statement", "ProviderFormNumber": "ICC24 CGSD3002-24", "s3Location": "appsign"},
    "915": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-NAIC", "s3Location": "consent", "state": ["WI", "WA", "IL", "GA"]},
    "916": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-ME", "s3Location": "consent", "state": ["ME"]},
    "932": {"FormName": "NAIC form", "ProviderFormNumber": "AFF_NAIC 8.25", "s3Location": "appsign", "state": ["AK","AL","AR","AZ","CO","CT","HI","IA","KY","LA","MD","ME","MO","MS","MT","NC","NE","NH","NJ","NM","OH","OR","RI","SC","SD","TX","UT","VA","VT","WI","WV"]}
  },
  "npw_offerexpired_pending_producer_cert": {
    "907": {"FormName": "Application for Whole Life Insurance", "ProviderFormNumber": "ICC24 CGA2000-24", "s3Location": "cip"},
    "910": {"FormName": "eConsent form", "ProviderFormNumber": "CD-001-2410", "s3Location": "appsign"},
    "911": {"FormName": "Authorization to Use or Disclose Protected Health Information (HIPAA form)", "ProviderFormNumber": "UW-CD-001-2408", "s3Location": "appsign"},
    "912": {"FormName": "Information Practices Related to Underwriting Your Application (MIB pre-notice and FCRA disclosure)", "ProviderFormNumber": "UW-CD-002-2408", "s3Location": "consent"},
    "913": {"FormName": "GLBA Notice (Privacy Policy)", "ProviderFormNumber": "CGIC-GLBA (1221)", "s3Location": "consent"},
    "914": {"FormName": "Terminal Illness Accelerated Death Benefit Rider Summary and Disclosure Statement", "ProviderFormNumber": "ICC24 CGSD3002-24", "s3Location": "cip"},
    "915": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-NAIC", "s3Location": "consent", "state": ["WI", "WA", "IL", "GA"]},
    "916": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-ME", "s3Location": "consent", "state": ["ME"]},
    "921": {"FormName": "Modified Endowment Contract Disclosure", "ProviderFormNumber": "LIFE-CD-001-2410", "s3Location": "cip"},
    "922": {"FormName": "Preliminary Statement of Costs", "ProviderFormNumber": "LIFE-CD-002-2410", "s3Location": "cip", "state": ["ME"]},
    "923": {"FormName": "PA Disclosure Statement", "ProviderFormNumber": "LIFE-CD-PA-001-2410", "s3Location": "appsign", "state": ["PA"]},
    "924": {"FormName": "eConsent form", "ProviderFormNumber": "CD-001-2410", "role": "otherPayor", "s3Location": "cip"},
    "932": {"FormName": "NAIC form", "ProviderFormNumber": "AFF_NAIC 8.25", "s3Location": "cip", "state": ["AK","AL","AR","AZ","CO","CT","HI","IA","KY","LA","MD","ME","MO","MS","MT","NC","NE","NH","NJ","NM","OH","OR","RI","SC","SD","TX","UT","VA","VT","WI","WV"]}
  },
  "npw_offerexpired_pending_signatures": {
    "907": {"FormName": "Application for Whole Life Insurance", "ProviderFormNumber": "ICC24 CGA2000-24", "s3Location": "appsign"},
    "910": {"FormName": "eConsent form", "ProviderFormNumber": "CD-001-2410", "s3Location": "appsign"},
    "911": {"FormName": "Authorization to Use or Disclose Protected Health Information (HIPAA form)", "ProviderFormNumber": "UW-CD-001-2408", "s3Location": "appsign"},
    "912": {"FormName": "Information Practices Related to Underwriting Your Application (MIB pre-notice and FCRA disclosure)", "ProviderFormNumber": "UW-CD-002-2408", "s3Location": "consent"},
    "913": {"FormName": "GLBA Notice (Privacy Policy)", "ProviderFormNumber": "CGIC-GLBA (1221)", "s3Location": "consent"},
    "914": {"FormName": "Terminal Illness Accelerated Death Benefit Rider Summary and Disclosure Statement", "ProviderFormNumber": "ICC24 CGSD3002-24", "s3Location": "appsign"},
    "915": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-NAIC", "s3Location": "consent", "state": ["WI", "WA", "IL", "GA"]},
    "916": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-ME", "s3Location": "consent", "state": ["ME"]},
    "924": {"FormName": "eConsent form", "ProviderFormNumber": "CD-001-2410", "role": "otherPayor", "s3Location": "appsign"},
    "932": {"FormName": "NAIC form", "ProviderFormNumber": "AFF_NAIC 8.25", "s3Location": "appsign", "state": ["AK","AL","AR","AZ","CO","CT","HI","IA","KY","LA","MD","ME","MO","MS","MT","NC","NE","NH","NJ","NM","OH","OR","RI","SC","SD","TX","UT","VA","VT","WI","WV"]}
  },
  "npw_payment_failure": {
    "907": {"FormName": "Application for Whole Life Insurance", "ProviderFormNumber": "ICC24 CGA2000-24", "s3Location": "appsign"},
    "910": {"FormName": "eConsent form", "ProviderFormNumber": "CD-001-2410", "s3Location": "appsign"},
    "911": {"FormName": "Authorization to Use or Disclose Protected Health Information (HIPAA form)", "ProviderFormNumber": "UW-CD-001-2408", "s3Location": "appsign"},
    "912": {"FormName": "Information Practices Related to Underwriting Your Application (MIB pre-notice and FCRA disclosure)", "ProviderFormNumber": "UW-CD-002-2408", "s3Location": "consent"},
    "913": {"FormName": "GLBA Notice (Privacy Policy)", "ProviderFormNumber": "CGIC-GLBA (1221)", "s3Location": "consent"},
    "914": {"FormName": "Terminal Illness Accelerated Death Benefit Rider Summary and Disclosure Statement", "ProviderFormNumber": "ICC24 CGSD3002-24", "s3Location": "appsign"},
    "915": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-NAIC", "s3Location": "consent", "state": ["WI", "WA", "IL", "GA"]},
    "916": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-ME", "s3Location": "consent", "state": ["ME"]},
    "932": {"FormName": "NAIC form", "ProviderFormNumber": "AFF_NAIC 8.25", "s3Location": "appsign", "state": ["AK","AL","AR","AZ","CO","CT","HI","IA","KY","LA","MD","ME","MO","MS","MT","NC","NE","NH","NJ","NM","OH","OR","RI","SC","SD","TX","UT","VA","VT","WI","WV"]}
  },
  "npw_timeout": {
    "907": {"FormName": "Application for Whole Life Insurance", "ProviderFormNumber": "ICC24 CGA2000-24", "s3Location": "appsign"},
    "910": {"FormName": "eConsent form", "ProviderFormNumber": "CD-001-2410", "s3Location": "appsign"},
    "911": {"FormName": "Authorization to Use or Disclose Protected Health Information (HIPAA form)", "ProviderFormNumber": "UW-CD-001-2408", "s3Location": "appsign"},
    "912": {"FormName": "Information Practices Related to Underwriting Your Application (MIB pre-notice and FCRA disclosure)", "ProviderFormNumber": "UW-CD-002-2408", "s3Location": "consent"},
    "913": {"FormName": "GLBA Notice (Privacy Policy)", "ProviderFormNumber": "CGIC-GLBA (1221)", "s3Location": "consent"},
    "914": {"FormName": "Terminal Illness Accelerated Death Benefit Rider Summary and Disclosure Statement", "ProviderFormNumber": "ICC24 CGSD3002-24", "s3Location": "appsign"},
    "915": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-NAIC", "s3Location": "consent", "state": ["WI", "WA", "IL", "GA"]},
    "916": {"FormName": "Life Insurance Buyer's Guide", "ProviderFormNumber": "BGL-ME", "s3Location": "consent", "state": ["ME"]},
    "932": {"FormName": "NAIC form", "ProviderFormNumber": "AFF_NAIC 8.25", "s3Location": "appsign", "state": ["AK","AL","AR","AZ","CO","CT","HI","IA","KY","LA","MD","ME","MO","MS","MT","NC","NE","NH","NJ","NM","OH","OR","RI","SC","SD","TX","UT","VA","VT","WI","WV"]}
  }
}
```

## Task 4 — `assistant/tools/arc_center_client.py`

```python
class ArcCenterContextClient:
    """
    Wraps the EXISTING generic arcCenter endpoint that returns application context
    (productId, arcidStatus, state) for an arcId.

    TODO before this is usable — none of the following are known yet, do not guess:
      - Real base URL (config: ARC_CENTER_API_BASE_URL env var, currently unset)
      - Real path / prsCode for this lookup (likely similar to 369RET per arcenter-engine
        skill docs, but not confirmed for this use case)
      - Auth mechanism (API key? service token? mTLS?)
      - Exact response field names (assumed productId/arcidStatus/state below — confirm
        against a real response before wiring this up)
    """
    def get_application_context(self, arc_id: str) -> dict:
        """
        Returns: {"productId": str, "arcidStatus": str, "state": str, "roles": list[str]}
        Raises NotImplementedError until the TODOs above are resolved.
        """
        raise NotImplementedError(
            "arcCenter generic endpoint contract not yet confirmed — see class docstring"
        )
```

## Task 5 — `assistant/tools/control_mapping_client.py`

IMPORTANT — this data is fetched LIVE on every call, never cached or mirrored. Unlike
`dim_products_mirror` (cosmetic names, fine to sync periodically), `pdfA103Mapping` content
determines which folder a PDF is expected to be in — if it changes and the agent answers from a
stale copy, that's a wrong audit answer, not a cosmetic staleness issue. `RestrictedControlAPIMappingSource`
must not add any caching layer (no TTL cache, no local copy, no scheduled sync). Every call to
`resolve_pdf_locations` re-fetches from Valkey via the endpoint at call time.

```python
from abc import ABC, abstractmethod

class MappingSource(ABC):
    @abstractmethod
    def get_pdf_mapping(self, product_id: str) -> dict:
        """Returns the full pdfA103Mapping-shaped dict for this product_id, fetched live —
        implementations must not cache this."""


class LocalConfigMappingSource(MappingSource):
    """
    DEV/TEST ONLY — never used in production. Reads a static local JSON file so the graph
    is runnable without live Valkey access during development.

    We currently only have real content for productId 511801, saved at
    assistant/config/pdf_mappings/511801.json. The other 4 productIds in this family
    (314005, 4127, 614004, 214005) do NOT have known content yet — calling this with any
    productId other than 511801 must raise a clear error (e.g. FileNotFoundError with a
    message naming the missing productId), not silently fall back to 511801's mapping.
    """
    def get_pdf_mapping(self, product_id: str) -> dict:
        ...  # load assistant/config/pdf_mappings/{product_id}.json, raise clearly if missing


class RestrictedControlAPIMappingSource(MappingSource):
    """
    PRODUCTION PATH. STUB for now — the restricted-access endpoint exposing
    CENTER<productId>CONTROL.pdfA103Mapping does not exist yet (being built separately).
    Once it exists: confirm URL/auth, implement as a live call with NO caching (see module
    docstring above — staleness here means wrong audit answers, unlike dim_products_mirror).
    The lookup key is product_id directly (confirmed: pdfA103Mapping is a static field name,
    not templated by a code name — see Context section).
    """
    def get_pdf_mapping(self, product_id: str) -> dict:
        raise NotImplementedError(
            "Restricted CENTER<productId>CONTROL API not yet built — see class docstring. "
            "When implemented, this must fetch live every call, no caching."
        )
```

## Task 6 — Replace `assistant/tools/consent_lookup.py` with `assistant/tools/pdf_resolver.py`

Delete `consent_lookup.py` (it predates this design) and create:

```python
KNOWN_STATUSES = {
    "decline", "final", "mibpend", "modifiedagedecline", "npw_aif",
    "npw_offerexpired_pending_payment", "npw_offerexpired_pending_producer_cert",
    "npw_offerexpired_pending_signatures", "npw_payment_failure", "npw_timeout",
}

class PdfResolutionResult:
    """One resolved (or explicitly non-applicable) form for an arcId."""
    form_number: str
    form_name: str
    provider_form_number: str
    applicable: bool          # False if filtered out by state/role
    bucket: str | None
    key: str | None
    file_name: str | None
    reason_not_applicable: str | None


def resolve_pdf_locations(
    arc_id: str,
    env: str,                          # "PROD" | "DEMO" | "DEV"
    form_numbers: list[str] | None,    # None = resolve all forms for the app's status
    context_client: "ArcCenterContextClient",
    mapping_source: "MappingSource",
) -> list[PdfResolutionResult]:
    """
    1. context = context_client.get_application_context(arc_id)
    2. If context["arcidStatus"] not in KNOWN_STATUSES: raise a distinct
       UnreliableStatusError (do NOT silently proceed) — this is what gets caught
       upstream and routed to the graph's `escalate` node with a note that
       arcidStatus looked wrong, per explicit instruction to flag bad status data.
    3. mapping = mapping_source.get_pdf_mapping(context["productId"])
    4. status_forms = mapping.get(context["arcidStatus"], {})
    5. target_forms = form_numbers or list(status_forms.keys())
    6. For each form_number in target_forms not in status_forms: skip (not applicable
       for this status at all).
    7. For each remaining form entry:
       - if "state" in entry and context["state"] not in entry["state"]:
         applicable=False, reason_not_applicable="state {state} not in {entry[state]}"
       - if "role" in entry and entry["role"] not in context.get("roles", []):
         applicable=False, reason_not_applicable="role {entry[role]} not present on this application"
       - else: applicable=True, bucket=resolve_bucket_name(env) [see Task 7],
         key=f"{entry['s3Location']}/{arc_id}{form_number}.pdf",
         file_name=f"{arc_id}{form_number}.pdf"
    8. Return list of PdfResolutionResult.
    """
```

Add a custom exception `UnreliableStatusError(Exception)` in the same file, raised per step 2
above — this must propagate distinctly from "not found" so the graph can tell "the data itself
looks wrong" apart from "this form doesn't apply here."

## Task 7 — `assistant/storage/` (S3 live, Azure stub)

```
assistant/storage/
  __init__.py
  base.py       # StorageBackend ABC: exists(bucket, key) -> bool; presigned_url(bucket, key, ttl) -> str
  s3_backend.py # boto3-based, real implementation
  azure_backend.py  # STUB, raises NotImplementedError, comment: "wire up when migration happens in ~2 weeks, container name TBD"
  bucket_config.py  # resolve_bucket_name(env) -> str
```

`bucket_config.py`:
```python
# TODO: real bucket names per env are not yet confirmed — these are placeholders.
# User confirmed "arc369 is the bucket name itself" but did not confirm the exact
# per-env naming convention (e.g. is DEMO a separate bucket "arc369-demo", or a
# prefix within the same bucket, or a separate AWS account entirely?). Do not treat
# these values as correct until confirmed.
BUCKET_BY_ENV = {
    "PROD": "arc369",       # TODO CONFIRM
    "DEMO": "arc369-demo",  # TODO CONFIRM
    "DEV": "arc369-dev",    # TODO CONFIRM
}

def resolve_bucket_name(env: str) -> str:
    if env not in BUCKET_BY_ENV:
        raise ValueError(f"Unknown env: {env}")
    return BUCKET_BY_ENV[env]
```

## Task 8 — Wire into the graph

In `assistant/graph.py`, replace the `structured_lookup` branch's call to the old
`get_consent_pdf` stub with `resolve_pdf_locations`. Catch `UnreliableStatusError` specifically
and route to `escalate` with a note in the escalation record: `"arcidStatus returned an
unrecognized value — flagging for review rather than guessing."` (per explicit instruction:
flag arcidStatus problems, don't silently proceed).

## Acceptance check

Running the existing `assistant/run_local.py` test question ("Where can I find the eConsent/HIPAA
signed PDF for arcId ARCF25344h646?") should now hit `resolve_pdf_locations`, which will raise
`NotImplementedError` from `ArcCenterContextClient` (Task 4 TODO) — confirm it's caught and routed
to `escalate` cleanly, not an unhandled crash. This is the correct behavior until Task 4's TODOs
are resolved with real endpoint details.

## Explicitly out of scope / blocked on external info

- `ArcCenterContextClient` real implementation — needs endpoint URL/auth/response shape.
- `RestrictedControlAPIMappingSource` real implementation — endpoint doesn't exist yet. This is
  the production path and must fetch live, no caching, once built.
- `AzureBlobBackend` real implementation — container details not final.
- `BUCKET_BY_ENV` real values — not confirmed.
- `dim_products_mirror` real column types and MySQL connection details — not confirmed.
- Confirmed: content behind `pdfA103Mapping` differs per productId even though the field name is
  static. We only have real content for productId `511801`. The other 4 (`314005`, `4127`,
  `614004`, `214005`) need their own confirmed mappings before they can be resolved — do not
  assume they match 511801.
