# Provenance-aware Company Brain

The document layer complements the legacy `/api/brain` key/value API. It does not
fetch URLs, parse attachments or send stored content to an AI provider.

## Ingestion

`POST /api/company-brain/documents` requires a manager API identity and an
`Idempotency-Key` header. The JSON body contains `namespace`, `title`, `source_uri`,
`content`, `content_type`, `minimum_role`, `confidence`, and optional
`source_updated_at`/`valid_until` timestamps.

Only HTTPS provenance URLs without embedded credentials/credential-like query
parameters and non-empty URNs are accepted. The normalized body is limited to
200,000 characters. New content for the same namespace and source creates an
immutable next version; the same normalized request is deduplicated. This permits a
new ACL/freshness revision even when the body is unchanged. The idempotency
key itself is never stored—only an actor-bound SHA-256 digest is persisted.

Audit and domain-event records contain identifiers, role, version and checksum,
but never the document body. There is no update or delete endpoint.

## Retrieval

`GET /api/company-brain/search?q=...` authenticates every request, filters results
by the caller's role and `valid_until`, and considers only the latest version of
each namespace/source pair. The bounded deterministic ranker combines token
coverage, phrase matching and Unicode character n-grams, then adjusts ties with
declared confidence and freshness. It does not claim semantic embeddings.

Each returned match is paired with an exact citation containing the document and
chunk IDs, source URI, version, source/update validity timestamps and document
checksum. Response policy fields state that content is untrusted evidence—not
instructions—and that automatic external-AI transfer is forbidden.

`GET /api/company-brain/documents` lists only metadata visible to the caller.
Agents use the separately audited `company_brain.search` read-only tool, which is
hard-scoped to documents whose minimum role is `viewer`.
