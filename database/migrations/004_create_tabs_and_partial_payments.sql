-- 004: comandas abertas, lançamentos incrementais, pagamentos parciais e encerramento (Bloco 3).
-- Regras de integridade:
-- - Identificador interno imutável (tabs.id) separado do número manual da comanda (tabs.number).
-- - Coluna gerada open_number garante no banco que não existam duas comandas abertas com o mesmo número,
--   permitindo reutilizar o número após o encerramento (quando open_number passa a NULL).
-- - Lançamentos de consumo usam sales (kind = 'tab_item', tab_id = tabs.id) e sale_items.
-- - Pagamentos parciais ou integrais da comanda usam cash_movements (kind = 'sale_payment', tab_id = tabs.id).

CREATE TABLE tabs (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    shift_id      INT UNSIGNED    NOT NULL,
    number        INT UNSIGNED    NOT NULL,
    customer_name VARCHAR(120)    NULL,
    status        ENUM('open', 'closed') NOT NULL DEFAULT 'open',
    opened_by     INT UNSIGNED    NOT NULL,
    opened_at     DATETIME        NOT NULL,
    closed_by     INT UNSIGNED    NULL,
    closed_at     DATETIME        NULL,
    notes         VARCHAR(255)    NULL,
    open_number   INT UNSIGNED GENERATED ALWAYS AS (IF(status = 'open', number, NULL)) PERSISTENT,
    PRIMARY KEY (id),
    CONSTRAINT uq_tabs_open_number UNIQUE (open_number),
    CONSTRAINT ck_tabs_number CHECK (number >= 1 AND number <= 999999),
    CONSTRAINT ck_tabs_closed_fields CHECK (
        (status = 'open' AND closed_at IS NULL AND closed_by IS NULL)
        OR (status = 'closed' AND closed_at IS NOT NULL AND closed_by IS NOT NULL)
    ),
    KEY ix_tabs_status_number (status, number),
    KEY ix_tabs_number_id (number, id),
    KEY ix_tabs_shift (shift_id),
    CONSTRAINT fk_tabs_shift FOREIGN KEY (shift_id) REFERENCES shifts (id),
    CONSTRAINT fk_tabs_opened_by FOREIGN KEY (opened_by) REFERENCES users (id),
    CONSTRAINT fk_tabs_closed_by FOREIGN KEY (closed_by) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE sales
    ADD COLUMN customer_refund_amount DECIMAL(10, 2) NOT NULL DEFAULT 0.00 AFTER customer_refund_status;

UPDATE sales
SET customer_refund_amount = total_charged
WHERE status = 'voided'
  AND customer_refund_status IN ('pending', 'completed')
  AND customer_refund_amount = 0.00;

ALTER TABLE sales
    DROP CONSTRAINT ck_sales_void_fields,
    ADD CONSTRAINT ck_sales_void_fields CHECK (
        (status = 'active' AND voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL
         AND customer_refund_status = 'not_applicable' AND customer_refund_amount = 0.00)
        OR (status = 'voided' AND kind = 'quick' AND voided_at IS NOT NULL AND voided_by IS NOT NULL
            AND void_reason IS NOT NULL AND customer_refund_status IN ('pending', 'completed')
            AND customer_refund_amount = total_charged)
        OR (status = 'voided' AND kind = 'tab_item' AND voided_at IS NOT NULL AND voided_by IS NOT NULL
            AND void_reason IS NOT NULL
            AND ((customer_refund_status = 'not_applicable' AND customer_refund_amount = 0.00)
                 OR (customer_refund_status IN ('pending', 'completed') AND customer_refund_amount > 0.00)))
    ),
    ADD CONSTRAINT ck_sales_kind_tab CHECK (
        (kind = 'quick' AND tab_id IS NULL)
        OR (kind = 'tab_item' AND tab_id IS NOT NULL AND card_fee_total = 0.00)
    ),
    ADD KEY ix_sales_tab (tab_id, id),
    ADD CONSTRAINT fk_sales_tab FOREIGN KEY (tab_id) REFERENCES tabs (id);

ALTER TABLE cash_movements
    ADD COLUMN batch_key VARCHAR(64) NULL AFTER idempotency_key,
    ADD COLUMN request_fingerprint CHAR(64) NULL AFTER batch_key,
    ADD KEY ix_cash_movements_tab (tab_id, id),
    ADD KEY ix_cash_movements_batch_key (batch_key),
    ADD KEY ix_cash_movements_reverses (reverses_id),
    DROP INDEX uq_cash_movements_reverses,
    ADD CONSTRAINT fk_cash_movements_tab FOREIGN KEY (tab_id) REFERENCES tabs (id),
    ADD CONSTRAINT ck_cash_movements_origin CHECK (
        (kind = 'sale_payment' AND ((sale_id IS NOT NULL AND tab_id IS NULL) OR (tab_id IS NOT NULL AND sale_id IS NULL)))
        OR (kind <> 'sale_payment')
    );
