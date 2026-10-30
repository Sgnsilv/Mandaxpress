# Mandaxpress — Modelo financeiro (implementado no Bloco 2 e preparado para Bloco 3+)

Status: **núcleo do Bloco 2 implementado e validado em MariaDB** (`003_create_products_and_quick_sales.sql`). Comandas abertas e créditos antecipados estão previstos no mesmo livro-razão (`cash_movements`) para os próximos blocos.

## 1. Princípio: quatro conceitos separados

| Conceito | O que é | Onde vive |
|---|---|---|
| **Venda / consumo** | Produtos cadastrados ou itens avulsos entregues ao cliente (receita de produtos) | `sales` + `sale_items` |
| **Taxa repassada** | Acréscimo de cartão cobrado do cliente além do subtotal dos produtos | `sales.card_fee_total` + `cash_movements.fee_amount` |
| **Movimento de caixa** | Valor que entrou ou saiu por meio (`pix`, `card`, `cash`) | `cash_movements` (livro-razão imutável) |
| **Obrigação com o cliente** | Devolução financeira pendente de venda estornada; e, nos próximos blocos, crédito antecipado/saldo de comanda | `sales.customer_refund_status` (Bloco 2) e saldos derivados (Bloco 3+) |

Regras de ouro:
- Venda é diferente de recebimento.
- Recebimento é diferente de crédito antecipado.
- Taxa repassada é diferente de valor dos produtos.
- Troco em dinheiro **não** é despesa operacional nem venda adicional.
- Estorno **não** apaga o histórico original e **não** presume devolução automática do dinheiro ao cliente.
- Todos os valores monetários são `Decimal` no Python e `DECIMAL(10, 2)` no MariaDB (nunca `float`).
- Preços e totais são sempre calculados e validados no backend.

## 2. Tabelas implementadas no Bloco 2 (`003_create_products_and_quick_sales.sql`)

```text
products(
  id, name UNIQUE, category, price DECIMAL(10,2) >= 0, is_active, created_at, updated_at
)
payment_settings(
  id = 1, card_fee_percent DECIMAL(5,2) DEFAULT 0.00,
  card_fee_mode ENUM('simple_addition', 'pass_through') DEFAULT 'simple_addition',
  updated_by, updated_at
)
shifts(
  id, status ENUM('open', 'closed'), opened_by, opened_at, opening_cash DECIMAL(10,2),
  closed_by, closed_at, counted_cash DECIMAL(10,2), notes
)
sales(
  id, shift_id, tab_id NULL, kind ENUM('quick', 'tab_item'), status ENUM('active', 'voided'),
  subtotal DECIMAL(10,2), card_fee_total DECIMAL(10,2), total_charged DECIMAL(10,2),
  operator_id, created_at, idempotency_key UNIQUE, request_fingerprint CHAR(64),
  voided_by, voided_at, void_reason,
  customer_refund_status ENUM('not_applicable', 'pending', 'completed'),
  customer_refunded_at, customer_refunded_by, customer_refund_method, customer_refund_notes
)
sale_items(
  id, sale_id, product_id NULL, item_kind ENUM('product', 'custom'),
  description, quantity >= 1, unit_price DECIMAL(10,2) >= 0, line_total DECIMAL(10,2)
)
cash_movements(
  id, shift_id, tab_id NULL, sale_id NULL,
  kind ENUM('sale_payment', 'credit_deposit', 'credit_refund', 'reversal'),
  method ENUM('pix', 'card', 'cash'), direction ENUM('in', 'out'),
  amount DECIMAL(10,2) > 0,
  fee_percent DECIMAL(5,2), fee_mode ENUM('none', 'simple_addition', 'pass_through'),
  fee_amount DECIMAL(10,2) >= 0, gross_amount DECIMAL(10,2) = amount + fee_amount,
  cash_tendered DECIMAL(10,2) NULL, change_given DECIMAL(10,2) NULL,
  reverses_id BIGINT UNSIGNED NULL UNIQUE, operator_id, created_at, idempotency_key UNIQUE, note
)
audit_logs(
  id, operator_id, action, entity, entity_id, reason, details, created_at
)
```

## 3. Modelo de pagamentos e taxa de cartão

### 3.1. Regra da taxa de cartão
- A taxa do cartão é **repassada integralmente ao cliente**.
- Configuração administrativa em `payment_settings` (valor inicial `0.00%`).
- A taxa incide **somente sobre a parcela de principal paga no cartão** (`method = 'card'`).
- Pagamentos em `pix` e `cash` têm `fee_percent = 0.00`, `fee_amount = 0.00` e `fee_mode = 'none'`.
- Em cada movimento são gravados os snapshots `fee_percent`, `fee_mode`, `fee_amount` e `gross_amount = amount + fee_amount`, preservando os valores históricos mesmo se a taxa for alterada depois.

### 3.2. Modos de cálculo suportados (`backend/src/domain/finance.py`)
1. **Acréscimo simples (`simple_addition` — padrão do MVP):**
   - $\text{fee\_amount} = \text{round\_half\_up}\left(\text{amount} \times \frac{\text{fee\_percent}}{100},\ 2\right)$
   - $\text{gross\_amount} = \text{amount} + \text{fee\_amount}$
   - Exemplo: Produtos = R$ 20,00, taxa = 3% → Principal = R$ 20,00, Taxa = R$ 0,60, Total cobrado = R$ 20,60.
2. **Repasse de desconto no bruto (`pass_through`):**
   - Caso a operadora da maquininha desconte $t\%$ sobre o valor bruto cobrado e o evento deseje receber exatamente o principal líquido:
   - $\text{gross\_amount} = \text{round\_half\_up}\left(\frac{\text{amount}}{1 - \text{fee\_percent}/100},\ 2\right)$
   - $\text{fee\_amount} = \text{gross\_amount} - \text{amount}$

### 3.3. Pagamento dividido com taxa
Para qualquer venda rápida:
$$\sum \text{payments.amount} = \text{sales.subtotal}$$
$$\sum \text{payments.fee\_amount} = \text{sales.card\_fee\_total}$$
$$\sum \text{payments.gross\_amount} = \text{sales.total\_charged} = \text{sales.subtotal} + \text{sales.card\_fee\_total}$$

Exemplo validado em teste automatizado:
- Produtos (`subtotal`): R$ 50,00.
- Parcela 1 (`pix`): `amount = 20.00`, `fee_amount = 0.00`, `gross_amount = 20.00`.
- Parcela 2 (`card`, taxa 3% em `simple_addition`): `amount = 30.00`, `fee_amount = 0.90`, `gross_amount = 30.90`.
- Total cobrado (`total_charged`): R$ 50,90 (nunca aplica taxa sobre a própria taxa nem sobre a parcela Pix).

## 4. Dinheiro entregue e troco

Em pagamentos em dinheiro (`method = 'cash'` e `direction = 'in'`):
- `amount` = parcela líquida destinada aos produtos (`gross_amount = amount`, pois não há taxa em dinheiro).
- `cash_tendered` = valor físico entregue pelo cliente (`>= amount`).
- `change_given` = troco devolvido (`cash_tendered - amount`).
- Garantido por `CHECK (cash_tendered >= gross_amount AND change_given = cash_tendered - gross_amount)`.

Exemplo validado em teste automatizado:
- Venda de R$ 32,00 paga com nota de R$ 50,00:
  - `sales.subtotal = 32.00`, `sales.total_charged = 32.00`
  - `cash_movements`: `amount = 32.00`, `gross_amount = 32.00`, `cash_tendered = 50.00`, `change_given = 18.00`
  - Entrada líquida em dinheiro no resumo financeiro = **R$ 32,00** (o troco não é descontado duas vezes nem tratado como venda adicional).

## 5. Estornos e distinção contábil × financeira

No Bloco 2, o estorno de venda rápida exige autenticação, perfil `admin`, motivo obrigatório e turno aberto (`shifts.status = 'open'`).

O modelo distingue explicitamente três eventos, **sem registrar saída de dinheiro no livro-razão antes da devolução efetiva**:

1. **Cancelamento contábil da venda:**
   - `sales.status` passa de `'active'` para `'voided'`, registrando `voided_by`, `voided_at` e `void_reason`.
   - Os itens em `sale_items` permanecem intactos para auditoria.
2. **Reversão contábil do recebimento reconhecido:**
   - A receita reconhecida por método (`recognized_principal`, `recognized_fee`, `recognized_gross`) considera apenas pagamentos ligados a vendas ativas (`s.status = 'active'`). Portanto, o cancelamento da venda reverte imediatamente o recebimento contábil reconhecido, sem precisar falsificar uma saída física de caixa que ainda não aconteceu.
3. **Devolução financeira efetiva ao cliente (`cash_movements`):**
   - Estornar uma venda no sistema **não** presume que o dinheiro já saiu do caixa ou foi devolvido ao cliente.
   - Ao estornar com `customer_refund_completed = False` (padrão seguro), a venda recebe `customer_refund_status = 'pending'`, somando em `pending_customer_refunds_total`, e **nenhum movimento `direction = 'out'`** é inserido em `cash_movements` (mantendo o saldo físico `net_gross` fiel ao dinheiro que ainda está na gaveta/conta).
   - Somente quando a devolução efetiva ao cliente é confirmada (`customer_refund_completed = True` no ato do estorno ou posteriormente em `POST /api/sales/<id>/customer-refund`), o sistema grava os movimentos compensatórios (`kind = 'reversal'`, `direction = 'out'`, `reverses_id = original.id` `UNIQUE`) em `cash_movements` e altera `customer_refund_status` para `'completed'`.

## 6. Transações e idempotência

- Toda confirmação de venda rápida ocorre dentro de uma única transação (`MariaDbUnitOfWork`), com `SELECT ... FOR UPDATE` nos registros lidos e rollback integral em qualquer erro.
- Cada tentativa lógica de confirmação envia `idempotency_key` (`UNIQUE` em `sales`) e o backend calcula `request_fingerprint` (SHA-256 canônico dos itens e pagamentos).
- Se a mesma chave for reenviada com o **mesmo payload**, o backend retorna a venda original sem duplicar registros (`200 OK`, `idempotent_replay = true`).
- Se a mesma chave for reutilizada com **payload diferente**, o backend rejeita com `409 Conflict` (`idempotency_conflict`).
