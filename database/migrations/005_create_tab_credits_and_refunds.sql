-- 005: crédito antecipado em comandas, aplicação ao consumo e devolução de saldo (Bloco 4).
-- Regras de integridade no livro-razão (cash_movements):
-- - Depósitos de crédito antecipado (kind = 'credit_deposit', direction = 'in') exigem tab_id IS NOT NULL e sale_id IS NULL.
-- - Devoluções de saldo de crédito (kind = 'credit_refund', direction = 'out') exigem tab_id IS NOT NULL e sale_id IS NULL.
-- - Pagamentos ordinários (kind = 'sale_payment', direction = 'in') exigem exatamente um entre sale_id ou tab_id.
-- - Reversões (kind = 'reversal', direction = 'out') exigem reverses_id IS NOT NULL e vínculo com venda ou comanda.
-- - Cancelamento administrativo de depósito de crédito (voided_at IS NOT NULL em kind = 'credit_deposit')
--   separa o cancelamento contábil do crédito (customer_refund_status = 'pending') da saída física de caixa
--   (customer_refund_status = 'completed' com movimento kind = 'reversal', direction = 'out').

ALTER TABLE cash_movements
    ADD COLUMN voided_by INT UNSIGNED NULL AFTER note,
    ADD COLUMN voided_at DATETIME NULL AFTER voided_by,
    ADD COLUMN void_reason VARCHAR(255) NULL AFTER voided_at,
    ADD COLUMN customer_refund_status ENUM('not_applicable', 'pending', 'completed') NOT NULL DEFAULT 'not_applicable' AFTER void_reason,
    ADD COLUMN customer_refund_amount DECIMAL(10, 2) NOT NULL DEFAULT 0.00 AFTER customer_refund_status,
    ADD COLUMN customer_refunded_at DATETIME NULL AFTER customer_refund_amount,
    ADD COLUMN customer_refunded_by INT UNSIGNED NULL AFTER customer_refunded_at,
    ADD COLUMN customer_refund_method ENUM('pix', 'card', 'cash') NULL AFTER customer_refunded_by,
    ADD COLUMN customer_refund_notes VARCHAR(255) NULL AFTER customer_refund_method,
    DROP CONSTRAINT ck_cash_movements_origin,
    ADD CONSTRAINT ck_cash_movements_origin CHECK (
        (kind = 'sale_payment' AND ((sale_id IS NOT NULL AND tab_id IS NULL) OR (tab_id IS NOT NULL AND sale_id IS NULL)))
        OR (kind IN ('credit_deposit', 'credit_refund') AND tab_id IS NOT NULL AND sale_id IS NULL)
        OR (kind = 'reversal' AND (sale_id IS NOT NULL OR tab_id IS NOT NULL))
    ),
    ADD CONSTRAINT ck_cash_movements_void_fields CHECK (
        (voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL
         AND customer_refund_status = 'not_applicable' AND customer_refund_amount = 0.00)
        OR (kind = 'credit_deposit' AND direction = 'in' AND voided_at IS NOT NULL AND voided_by IS NOT NULL
            AND void_reason IS NOT NULL AND customer_refund_status IN ('pending', 'completed')
            AND customer_refund_amount = amount)
    ),
    ADD CONSTRAINT fk_cash_movements_voided_by FOREIGN KEY (voided_by) REFERENCES users (id),
    ADD CONSTRAINT fk_cash_movements_refunded_by FOREIGN KEY (customer_refunded_by) REFERENCES users (id),
    ADD KEY ix_cash_movements_tab_kind (tab_id, kind, direction);
