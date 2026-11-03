-- BLINK AI PostgreSQL schema — RECONSTRUCTED 2026-10-21
--
-- The original migrations (database/postgres/migrations/000*.sql) were not in the
-- source export. This file is derived from every SQL statement in the backend
-- (repositories, auth services, admin routes, scripts) and SCHEMA.md.
-- Column types are inferred; review before loading any restored data.
-- Idempotent: safe to run more than once on an empty or partially built database.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- ── identity ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    status      text NOT NULL DEFAULT 'active'
                CHECK (status IN ('pending', 'active', 'suspended', 'deleted')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);
CREATE INDEX IF NOT EXISTS idx_users_created_at ON users (created_at DESC);

CREATE TABLE IF NOT EXISTS usernames (
    user_id     uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    username    text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_usernames_lower ON usernames (lower(username));

CREATE TABLE IF NOT EXISTS username_history (
    id            bigserial PRIMARY KEY,
    user_id       uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    old_username  text NOT NULL,
    changed_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_username_history_user ON username_history (user_id, changed_at DESC);

CREATE TABLE IF NOT EXISTS user_emails (
    id           bigserial PRIMARY KEY,
    user_id      uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    email        text NOT NULL,
    is_primary   boolean NOT NULL DEFAULT false,
    verified_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_emails_lower ON user_emails (lower(email));
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_emails_one_primary ON user_emails (user_id) WHERE is_primary;

CREATE TABLE IF NOT EXISTS password_credentials (
    user_id              uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    password_hash        text NOT NULL,
    hash_algorithm       text NOT NULL DEFAULT 'argon2id',
    password_changed_at  timestamptz NOT NULL DEFAULT now(),
    failed_attempts      integer NOT NULL DEFAULT 0,
    locked_until         timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS auth_identities (
    id                       bigserial PRIMARY KEY,
    user_id                  uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    provider                 text NOT NULL CHECK (provider IN ('google', 'line')),
    provider_subject         text NOT NULL,
    provider_email           text,
    provider_email_verified  boolean NOT NULL DEFAULT false,
    display_name             text,
    avatar_url               text,
    linked_at                timestamptz NOT NULL DEFAULT now(),
    last_login_at            timestamptz,
    UNIQUE (provider, provider_subject),
    UNIQUE (user_id, provider)
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id            bigserial PRIMARY KEY,
    user_id       uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash    text NOT NULL UNIQUE,
    auth_method   text NOT NULL,
    user_agent    text,
    ip_address    inet,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    last_seen_at  timestamptz,
    revoked_at    timestamptz
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_live ON auth_sessions (user_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS password_reset_challenges (
    id             uuid PRIMARY KEY,
    user_id        uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    code_hash      text NOT NULL,
    attempt_count  integer NOT NULL DEFAULT 0,
    max_attempts   integer NOT NULL DEFAULT 5,
    expires_at     timestamptz NOT NULL,
    consumed_at    timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_password_reset_user_time ON password_reset_challenges (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS email_registration_challenges (
    id             uuid PRIMARY KEY,
    email          text NOT NULL,
    code_hash      text NOT NULL,
    attempt_count  integer NOT NULL DEFAULT 0,
    max_attempts   integer NOT NULL DEFAULT 5,
    expires_at     timestamptz NOT NULL,
    consumed_at    timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_email_registration_email_time
    ON email_registration_challenges (lower(email), created_at DESC);

-- ── profile & preferences ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id       uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    display_name  text,
    gender        text,
    birthday      date,
    profile_data  jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id               uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    preferred_categories  text[] NOT NULL DEFAULT ARRAY[]::text[],
    disliked_ingredients  text[] NOT NULL DEFAULT ARRAY[]::text[],
    budget_min            numeric,
    budget_max            numeric,
    preferences_data      jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_behavior_events (
    id               bigserial PRIMARY KEY,
    user_id          uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    conversation_id  text,
    event_type       text NOT NULL,
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_behavior_events_user ON user_behavior_events (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS user_analysis_profiles (
    user_id            uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    profile            jsonb NOT NULL DEFAULT '{}'::jsonb,
    total_event_count  integer NOT NULL DEFAULT 0,
    analyzed_at        timestamptz,
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- ── conversations (bodies live in Zep) ─────────────────────────────
CREATE TABLE IF NOT EXISTS external_memory_links (
    user_id           uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    provider          text NOT NULL,
    external_user_id  text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, provider)
);

CREATE TABLE IF NOT EXISTS conversations (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    zep_session_id          text,
    client_conversation_id  text,
    title                   text,
    flow_type               text,
    started_at              timestamptz NOT NULL DEFAULT now(),
    last_message_at         timestamptz NOT NULL DEFAULT now(),
    archived_at             timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_client_id
    ON conversations (user_id, client_conversation_id) WHERE client_conversation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_conversations_user_recent ON conversations (user_id, last_message_at DESC);

-- ── quiz ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS quiz_attempts (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    quiz_key      text NOT NULL,
    quiz_version  text,
    status        text NOT NULL DEFAULT 'started',
    result_data   jsonb,
    started_at    timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz
);
CREATE INDEX IF NOT EXISTS idx_quiz_attempts_user ON quiz_attempts (user_id, completed_at DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS quiz_answers (
    id            bigserial PRIMARY KEY,
    attempt_id    uuid NOT NULL REFERENCES quiz_attempts (id) ON DELETE CASCADE,
    question_key  text NOT NULL,
    answer_data   jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- ── skin records & check-ins ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS skin_records (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    source       text NOT NULL DEFAULT 'chat',
    concern      text,
    severity     smallint CHECK (severity BETWEEN 1 AND 5),
    started_on   date,
    record_data  jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
-- text::date is not IMMUTABLE (depends on DateStyle); ISO 'YYYY-MM-DD' strings sort identically.
CREATE INDEX IF NOT EXISTS idx_skin_records_user_date
    ON skin_records (user_id, (record_data->>'date') DESC);

CREATE TABLE IF NOT EXISTS skin_record_areas (
    skin_record_id  uuid NOT NULL REFERENCES skin_records (id) ON DELETE CASCADE,
    area_code       text NOT NULL,
    PRIMARY KEY (skin_record_id, area_code)
);

CREATE TABLE IF NOT EXISTS daily_checkins (
    id            bigserial PRIMARY KEY,
    user_id       uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    checkin_date  date NOT NULL,
    mood_score    smallint,
    skin_status   jsonb,
    notes         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, checkin_date)
);

-- ── editorial content ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_contents (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content_type         text NOT NULL CHECK (content_type IN ('beauty_tip', 'dermatologist_faq')),
    title                text NOT NULL,
    summary              text NOT NULL,
    source_kind          text NOT NULL DEFAULT 'manual',
    source_name          text,
    source_url           text,
    source_published_at  timestamptz,
    review_status        text NOT NULL DEFAULT 'pending_review'
                         CHECK (review_status IN ('pending_review', 'in_review', 'published', 'rejected', 'archived')),
    reviewer_notes       text,
    is_active            boolean NOT NULL DEFAULT false,
    display_priority     integer NOT NULL DEFAULT 0,
    content_hash         text UNIQUE,
    metadata             jsonb NOT NULL DEFAULT '{}'::jsonb,
    published_at         timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_daily_contents_serving
    ON daily_contents (content_type) WHERE review_status = 'published' AND is_active;

-- ── assets, audit, lexicon ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS uploaded_assets (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    storage_key   text NOT NULL,
    content_type  text,
    byte_size     bigint,
    metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz
);

CREATE TABLE IF NOT EXISTS audit_events (
    id             uuid PRIMARY KEY,
    actor_user_id  uuid REFERENCES users (id) ON DELETE SET NULL,
    event_type     text NOT NULL,
    target_type    text,
    target_id      text,
    metadata       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS taiwan_lexicon_sources (
    id       bigserial PRIMARY KEY,
    dataset  text NOT NULL,
    name     text,
    url      text NOT NULL,
    version  text NOT NULL,
    UNIQUE (dataset, url, version)
);

CREATE TABLE IF NOT EXISTS taiwan_local_terms (
    id               bigserial PRIMARY KEY,
    dataset          text NOT NULL,
    dataset_item_id  text NOT NULL,
    term             text NOT NULL,
    normalized_term  text,
    category         text,
    canonical        text,
    meaning          text,
    handling         text,
    risk             text,
    action           text,
    note             text,
    version          text NOT NULL,
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    active           boolean NOT NULL DEFAULT true,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (dataset, dataset_item_id, version)
);

-- ── usage / cost tracking (admin) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS api_usage_events (
    id                   bigserial PRIMARY KEY,
    occurred_at          timestamptz NOT NULL DEFAULT now(),
    category             text NOT NULL,
    provider             text NOT NULL,
    service              text,
    operation            text,
    status               text,
    request_id           text,
    input_tokens         bigint,
    cached_input_tokens  bigint,
    output_tokens        bigint,
    total_tokens         bigint,
    quantity             numeric,
    unit_type            text,
    latency_ms           numeric,
    error_type           text,
    metadata             jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_api_usage_events_time ON api_usage_events (occurred_at DESC);

CREATE TABLE IF NOT EXISTS api_usage_pricing (
    id                            bigserial PRIMARY KEY,
    category                      text NOT NULL,
    provider                      text NOT NULL,
    service_pattern               text NOT NULL DEFAULT '*',
    operation_pattern             text NOT NULL DEFAULT '*',
    currency                      text NOT NULL DEFAULT 'USD',
    input_usd_per_million         numeric,
    cached_input_usd_per_million  numeric,
    output_usd_per_million        numeric,
    usd_per_unit                  numeric,
    active                        boolean NOT NULL DEFAULT true,
    effective_from                timestamptz NOT NULL DEFAULT now(),
    updated_at                    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_api_usage_pricing_active
    ON api_usage_pricing (category, provider, service_pattern, operation_pattern) WHERE active = TRUE;

CREATE TABLE IF NOT EXISTS admin_gpu_samples (
    id                   bigserial PRIMARY KEY,
    sampled_at           timestamptz NOT NULL,
    device_id            text NOT NULL,
    device_name          text,
    utilization_percent  double precision,
    vram_percent         double precision,
    vram_used_mb         double precision,
    vram_total_mb        double precision,
    temperature_c        double precision,
    power_w              double precision,
    processes            jsonb
);
CREATE INDEX IF NOT EXISTS idx_admin_gpu_samples_device_time ON admin_gpu_samples (device_id, sampled_at DESC);

COMMIT;
