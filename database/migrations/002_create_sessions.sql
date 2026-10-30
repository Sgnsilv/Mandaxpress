-- 002: sessões de login no servidor (cookie opaco; somente o hash é guardado).
-- Permite logout real, revogação e expiração. Dados não financeiros: podem ser
-- removidos após expirar.
CREATE TABLE sessions (
    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    token_hash   CHAR(64)     NOT NULL,
    csrf_token   VARCHAR(64)  NOT NULL,
    user_id      INT UNSIGNED NOT NULL,
    created_at   DATETIME     NOT NULL,
    last_seen_at DATETIME     NOT NULL,
    expires_at   DATETIME     NOT NULL,
    revoked_at   DATETIME     NULL,
    user_agent   VARCHAR(255) NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_sessions_token_hash UNIQUE (token_hash),
    KEY ix_sessions_user (user_id),
    KEY ix_sessions_expires (expires_at),
    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
