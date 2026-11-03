-- Durable PostgreSQL mirror for user-owned chat messages.
CREATE TABLE IF NOT EXISTS conversation_messages (
    id                  bigserial PRIMARY KEY,
    conversation_id     uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role                text NOT NULL CHECK (role IN ('user', 'assistant')),
    content             text NOT NULL CHECK (length(btrim(content)) > 0),
    request_id          text,
    source              text NOT NULL DEFAULT 'api' CHECK (source IN ('api', 'zep_backfill')),
    external_message_id text,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_messages_request_role
    ON conversation_messages (conversation_id, request_id, role)
    WHERE request_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_messages_external
    ON conversation_messages (conversation_id, source, external_message_id)
    WHERE external_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_conversation_messages_history
    ON conversation_messages (conversation_id, id);
