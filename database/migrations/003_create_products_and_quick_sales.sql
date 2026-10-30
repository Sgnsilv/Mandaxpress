-- 003: núcleo financeiro do Bloco 2 (produtos, turnos, configurações, vendas,
-- movimentos de caixa, estornos e trilha de auditoria).
-- Regras de integridade:
-- - Todos os valores monetários são DECIMAL(10, 2) (nunca FLOAT/DOUBLE).
-- - Venda (sales/sale_items) é separada de movimento de caixa (cash_movements).
-- - Taxa de cartão (fee_amount) é registrada separadamente do principal dos produtos (amount).
-- - Valor entregue em dinheiro (cash_tendered) e troco (change_given) ficam registrados
--   sem inflar o valor líquido reconhecido da venda (amount = cash_tendered - change_given).
-- - Estorno preserva o histórico original e diferencia cancelamento contábil,
--   reversão de recebimento e devolução efetiva ao cliente.

CREATE TABLE products (
    id         INT UNSIGNED   NOT NULL AUTO_INCREMENT,
    name       VARCHAR(120)   NOT NULL,
    category   VARCHAR(80)    NOT NULL DEFAULT 'Geral',
    price      DECIMAL(10, 2) NOT NULL,
    is_active  TINYINT(1)     NOT NULL DEFAULT 1,
    created_at DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    CONSTRAINT uq_products_name UNIQUE (name),
    CONSTRAINT ck_products_price CHECK (price >= 0.00),
    KEY ix_products_active_name (is_active, name),
    KEY ix_products_category (category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE payment_settings (
    id                 TINYINT UNSIGNED NOT NULL,
    card_fee_percent  DECIMAL(5, 2)    NOT NULL DEFAULT 0.00,
    card_fee_mode     ENUM('simple_addition', 'pass_through') NOT NULL DEFAULT 'simple_addition',
    updated_by        INT UNSIGNED     NULL,
    updated_at        DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    CONSTRAINT ck_payment_settings_singleton CHECK (id = 1),
    CONSTRAINT ck_payment_settings_fee_range CHECK (card_fee_percent >= 0.00 AND card_fee_percent < 100.00),
    CONSTRAINT fk_payment_settings_user FOREIGN KEY (updated_by) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO payment_settings (id, card_fee_percent, card_fee_mode)
VALUES (1, 0.00, 'simple_addition');

CREATE TABLE shifts (
    id           INT UNSIGNED   NOT NULL AUTO_INCREMENT,
    status       ENUM('open', 'closed') NOT NULL DEFAULT 'open',
    opened_by    INT UNSIGNED   NOT NULL,
    opened_at    DATETIME       NOT NULL,
    opening_cash DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    closed_by    INT UNSIGNED   NULL,
    closed_at    DATETIME       NULL,
    counted_cash DECIMAL(10, 2) NULL,
    notes        VARCHAR(500)   NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_shifts_opening_cash CHECK (opening_cash >= 0.00),
    CONSTRAINT ck_shifts_closed_fields CHECK (
        (status = 'open' AND closed_at IS NULL)
        OR (status = 'closed' AND closed_at IS NOT NULL)
    ),
    KEY ix_shifts_status (status),
    CONSTRAINT fk_shifts_opened_by FOREIGN KEY (opened_by) REFERENCES users (id),
    CONSTRAINT fk_shifts_closed_by FOREIGN KEY (closed_by) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE sales (
    id                     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    shift_id               INT UNSIGNED    NOT NULL,
    tab_id                 BIGINT UNSIGNED NULL,
    kind                   ENUM('quick', 'tab_item') NOT NULL DEFAULT 'quick',
    status                 ENUM('active', 'voided')  NOT NULL DEFAULT 'active',
    subtotal               DECIMAL(10, 2)  NOT NULL,
    card_fee_total         DECIMAL(10, 2)  NOT NULL DEFAULT 0.00,
    total_charged          DECIMAL(10, 2)  NOT NULL,
    operator_id            INT UNSIGNED    NOT NULL,
    created_at             DATETIME        NOT NULL,
    idempotency_key        VARCHAR(64)     NOT NULL,
    request_fingerprint    CHAR(64)        NOT NULL,
    voided_by              INT UNSIGNED    NULL,
    voided_at              DATETIME        NULL,
    void_reason            VARCHAR(255)    NULL,
    customer_refund_status ENUM('not_applicable', 'pending', 'completed') NOT NULL DEFAULT 'not_applicable',
    customer_refunded_at   DATETIME        NULL,
    customer_refunded_by   INT UNSIGNED    NULL,
    customer_refund_method ENUM('pix', 'card', 'cash') NULL,
    customer_refund_notes  VARCHAR(255)    NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_sales_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT ck_sales_amounts CHECK (
        subtotal > 0.00
        AND card_fee_total >= 0.00
        AND total_charged = subtotal + card_fee_total
    ),
    CONSTRAINT ck_sales_void_fields CHECK (
        (status = 'active' AND voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL AND customer_refund_status = 'not_applicable')
        OR (status = 'voided' AND voided_at IS NOT NULL AND voided_by IS NOT NULL AND void_reason IS NOT NULL AND customer_refund_status IN ('pending', 'completed'))
    ),
    KEY ix_sales_shift_created (shift_id, created_at),
    KEY ix_sales_status_created (status, created_at),
    CONSTRAINT fk_sales_shift FOREIGN KEY (shift_id) REFERENCES shifts (id),
    CONSTRAINT fk_sales_operator FOREIGN KEY (operator_id) REFERENCES users (id),
    CONSTRAINT fk_sales_voided_by FOREIGN KEY (voided_by) REFERENCES users (id),
    CONSTRAINT fk_sales_refunded_by FOREIGN KEY (customer_refunded_by) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE sale_items (
    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    sale_id      BIGINT UNSIGNED NOT NULL,
    product_id   INT UNSIGNED    NULL,
    item_kind    ENUM('product', 'custom') NOT NULL,
    description  VARCHAR(150)    NOT NULL,
    quantity     INT UNSIGNED    NOT NULL,
    unit_price   DECIMAL(10, 2)  NOT NULL,
    line_total   DECIMAL(10, 2)  NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_sale_items_kind_product CHECK (
        (item_kind = 'product' AND product_id IS NOT NULL)
        OR (item_kind = 'custom' AND product_id IS NULL)
    ),
    CONSTRAINT ck_sale_items_amounts CHECK (
        quantity >= 1
        AND unit_price >= 0.00
        AND line_total = quantity * unit_price
    ),
    KEY ix_sale_items_sale (sale_id),
    KEY ix_sale_items_product (product_id),
    CONSTRAINT fk_sale_items_sale FOREIGN KEY (sale_id) REFERENCES sales (id),
    CONSTRAINT fk_sale_items_product FOREIGN KEY (product_id) REFERENCES products (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE cash_movements (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    shift_id        INT UNSIGNED    NOT NULL,
    tab_id          BIGINT UNSIGNED NULL,
    sale_id         BIGINT UNSIGNED NULL,
    kind            ENUM('sale_payment', 'credit_deposit', 'credit_refund', 'reversal') NOT NULL,
    method          ENUM('pix', 'card', 'cash') NOT NULL,
    direction       ENUM('in', 'out') NOT NULL,
    amount          DECIMAL(10, 2)  NOT NULL,
    fee_percent     DECIMAL(5, 2)   NOT NULL DEFAULT 0.00,
    fee_mode        ENUM('none', 'simple_addition', 'pass_through') NOT NULL DEFAULT 'none',
    fee_amount      DECIMAL(10, 2)  NOT NULL DEFAULT 0.00,
    gross_amount    DECIMAL(10, 2)  NOT NULL,
    cash_tendered   DECIMAL(10, 2)  NULL,
    change_given    DECIMAL(10, 2)  NULL,
    reverses_id     BIGINT UNSIGNED NULL,
    operator_id     INT UNSIGNED    NOT NULL,
    created_at      DATETIME        NOT NULL,
    idempotency_key VARCHAR(64)     NOT NULL,
    note            VARCHAR(255)    NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_cash_movements_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT uq_cash_movements_reverses UNIQUE (reverses_id),
    CONSTRAINT ck_cash_movements_amounts CHECK (
        amount > 0.00
        AND fee_percent >= 0.00
        AND fee_amount >= 0.00
        AND gross_amount = amount + fee_amount
    ),
    CONSTRAINT ck_cash_movements_fee_method CHECK (
        (method = 'card')
        OR (fee_percent = 0.00 AND fee_amount = 0.00 AND fee_mode = 'none')
    ),
    CONSTRAINT ck_cash_movements_cash_change CHECK (
        (method = 'cash' AND direction = 'in' AND cash_tendered IS NOT NULL AND change_given IS NOT NULL
         AND cash_tendered >= gross_amount AND change_given = cash_tendered - gross_amount)
        OR (cash_tendered IS NULL AND change_given IS NULL)
    ),
    CONSTRAINT ck_cash_movements_kind_direction CHECK (
        (kind IN ('sale_payment', 'credit_deposit') AND direction = 'in' AND reverses_id IS NULL)
        OR (kind = 'credit_refund' AND direction = 'out' AND reverses_id IS NULL)
        OR (kind = 'reversal' AND reverses_id IS NOT NULL)
    ),
    KEY ix_cash_movements_shift (shift_id, created_at),
    KEY ix_cash_movements_sale (sale_id),
    CONSTRAINT fk_cash_movements_shift FOREIGN KEY (shift_id) REFERENCES shifts (id),
    CONSTRAINT fk_cash_movements_sale FOREIGN KEY (sale_id) REFERENCES sales (id),
    CONSTRAINT fk_cash_movements_reverses FOREIGN KEY (reverses_id) REFERENCES cash_movements (id),
    CONSTRAINT fk_cash_movements_operator FOREIGN KEY (operator_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE audit_logs (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    operator_id INT UNSIGNED    NOT NULL,
    action      VARCHAR(60)     NOT NULL,
    entity      VARCHAR(40)     NOT NULL,
    entity_id   BIGINT UNSIGNED NOT NULL,
    reason      VARCHAR(255)    NULL,
    details     TEXT            NULL,
    created_at  DATETIME        NOT NULL,
    PRIMARY KEY (id),
    KEY ix_audit_logs_entity (entity, entity_id),
    KEY ix_audit_logs_created (created_at),
    CONSTRAINT fk_audit_logs_operator FOREIGN KEY (operator_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
