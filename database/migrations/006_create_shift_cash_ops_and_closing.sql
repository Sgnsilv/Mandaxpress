-- 006: abertura explícita de turno, sangria, suprimento, conferência por método e fechamento imutável (Bloco 5).
-- Regras de integridade:
-- - Apenas um turno pode estar aberto simultaneamente no MVP (open_singleton UNIQUE).
-- - Sangrias (kind = 'cash_withdrawal', direction = 'out') e suprimentos (kind = 'cash_supply', direction = 'in')
--   são movimentos exclusivos em dinheiro (method = 'cash'), sem vínculo com venda ou comanda (sale_id IS NULL, tab_id IS NULL),
--   exigindo motivo (note IS NOT NULL) e preservando separação estrita de vendas, depósitos e devoluções de clientes.
-- - O encerramento grava valores esperados, valores conferidos, diferenças, justificativa e snapshot JSON imutável.

UPDATE shifts s
JOIN (SELECT MAX(id) AS keep_id FROM shifts WHERE status = 'open') latest
SET s.status = 'closed',
    s.closed_by = s.opened_by,
    s.closed_at = COALESCE(s.closed_at, CURRENT_TIMESTAMP),
    s.notes = COALESCE(s.notes, 'Encerrado na migracao de unicidade de turno')
WHERE s.status = 'open' AND s.id <> latest.keep_id;

ALTER TABLE shifts
    ADD COLUMN open_idempotency_key VARCHAR(64) NULL AFTER opening_cash,
    ADD COLUMN open_request_fingerprint CHAR(64) NULL AFTER open_idempotency_key,
    ADD COLUMN expected_cash DECIMAL(10, 2) NULL AFTER closed_at,
    ADD COLUMN cash_difference DECIMAL(10, 2) NULL AFTER counted_cash,
    ADD COLUMN expected_pix DECIMAL(10, 2) NULL AFTER cash_difference,
    ADD COLUMN counted_pix DECIMAL(10, 2) NULL AFTER expected_pix,
    ADD COLUMN pix_difference DECIMAL(10, 2) NULL AFTER counted_pix,
    ADD COLUMN expected_card DECIMAL(10, 2) NULL AFTER pix_difference,
    ADD COLUMN counted_card DECIMAL(10, 2) NULL AFTER expected_card,
    ADD COLUMN card_difference DECIMAL(10, 2) NULL AFTER counted_card,
    ADD COLUMN discrepancy_justification VARCHAR(500) NULL AFTER notes,
    ADD COLUMN close_idempotency_key VARCHAR(64) NULL AFTER discrepancy_justification,
    ADD COLUMN close_request_fingerprint CHAR(64) NULL AFTER close_idempotency_key,
    ADD COLUMN closing_snapshot LONGTEXT NULL AFTER close_request_fingerprint,
    ADD COLUMN open_singleton TINYINT UNSIGNED GENERATED ALWAYS AS (IF(status = 'open', 1, NULL)) PERSISTENT,
    ADD CONSTRAINT uq_shifts_open_singleton UNIQUE (open_singleton),
    ADD CONSTRAINT uq_shifts_open_idempotency_key UNIQUE (open_idempotency_key),
    ADD CONSTRAINT uq_shifts_close_idempotency_key UNIQUE (close_idempotency_key),
    ADD CONSTRAINT ck_shifts_counted_non_negative CHECK (
        (counted_cash IS NULL OR counted_cash >= 0.00)
        AND (counted_pix IS NULL OR counted_pix >= 0.00)
        AND (counted_card IS NULL OR counted_card >= 0.00)
    );

ALTER TABLE cash_movements
    MODIFY COLUMN kind ENUM(
        'sale_payment',
        'credit_deposit',
        'credit_refund',
        'reversal',
        'cash_supply',
        'cash_withdrawal'
    ) NOT NULL,
    DROP CONSTRAINT ck_cash_movements_kind_direction,
    ADD CONSTRAINT ck_cash_movements_kind_direction CHECK (
        (kind IN ('sale_payment', 'credit_deposit', 'cash_supply') AND direction = 'in' AND reverses_id IS NULL)
        OR (kind IN ('credit_refund', 'cash_withdrawal') AND direction = 'out' AND reverses_id IS NULL)
        OR (kind = 'reversal' AND reverses_id IS NOT NULL)
    ),
    DROP CONSTRAINT ck_cash_movements_origin,
    ADD CONSTRAINT ck_cash_movements_origin CHECK (
        (kind = 'sale_payment' AND ((sale_id IS NOT NULL AND tab_id IS NULL) OR (tab_id IS NOT NULL AND sale_id IS NULL)))
        OR (kind IN ('credit_deposit', 'credit_refund') AND tab_id IS NOT NULL AND sale_id IS NULL)
        OR (kind = 'reversal' AND (sale_id IS NOT NULL OR tab_id IS NOT NULL))
        OR (kind IN ('cash_supply', 'cash_withdrawal') AND sale_id IS NULL AND tab_id IS NULL AND method = 'cash' AND note IS NOT NULL)
    );
