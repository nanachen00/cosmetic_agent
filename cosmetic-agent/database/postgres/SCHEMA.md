# BLINK AI PostgreSQL relational model

PostgreSQL is the source of truth for account identity and structured user data.
Neo4j remains the product/ingredient/skin-concern knowledge graph. Zep remains
the conversation-memory store. Every integration is keyed by `users.id`.

## Relationship groups

- `users` 1:1 `usernames`, `password_credentials`, `user_profiles`,
  `user_preferences`.
- `users` 1:N `user_emails`, `auth_identities`, `auth_sessions`, challenges,
  consents, check-ins, quiz attempts, skin records, reports and conversations.
- `quiz_attempts` 1:N `quiz_answers`.
- `skin_records` N:M face-area codes through `skin_record_areas`.
- `users` 1:N external assets and 1:1 per memory provider through
  `external_memory_links`.
- `daily_contents` is independent editorial content. It stores reviewed
  `beauty_tip` and `dermatologist_faq` cards plus provenance and publication
  state; it contains no user data.

## Identity invariants

1. `users.id` is the only immutable cross-system user identifier.
2. A normalized email can belong to only one user.
3. A Google or LINE provider subject can belong to only one user.
4. A user can bind at most one identity from each OAuth provider.
5. OAuth-only users have no row in `password_credentials`; no default password
   is generated.
6. Verification and reset codes are represented only by hashes and are
   single-use through `consumed_at`.
7. Zep uses `users.id` via `external_memory_links`; email and username are not
   used as memory keys.

## Data deliberately excluded

- Product, ingredient, regulation and recommendation relationships stay in
  Neo4j.
- Message bodies and summaries stay in Zep. `conversations` stores only
  ownership and lookup metadata.
- Uploaded image bytes stay in object storage; PostgreSQL stores metadata.
- Crawled or graph-derived daily content enters PostgreSQL as
  `pending_review`; only active `published` rows are served to the frontend.

## Migration rule

This file is a schema proposal and must first be applied to a new development
database. Do not run it against production until the Neo4j/MongoDB migration
preview has been reviewed and a verified backup/rollback point exists.
