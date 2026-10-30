# Mandaxpress — Modelo financeiro (Blocos 2 e 3 implementados; preparado para Blocos 4 e 5)

Status: **núcleo dos Blocos 2 e 3 implementado e validado em MariaDB** (`003_create_products_and_quick_sales.sql` e `004_create_tabs_and_partial_payments.sql`). Créditos antecipados (Bloco 4) e fechamento completo de turno (Bloco 5) estão previstos no mesmo livro-razão (`cash_movements`).

## 1. Princípio: quatro conceitos separados

| Conceito | O que é | Onde vive |
|---|---|---|
| **Venda / consumo** | Produtos cadastrados ou itens avulsos entregues ao cliente (receita de produtos, tanto em venda rápida quanto em lançamentos de comanda) | `sales` (`kind IN ('quick', 'tab_item')`) + `sale_items` |
| **Taxa repassada** | Acréscimo de cartão cobrado do cliente além do principal dos produtos | `sales.card_fee_total` (venda rápida) + `cash_movements.fee_amount` (ambos) |
| **Movimento de caixa** | Valor que entrou ou saiu por meio (`pix`, `card`, `cash`) | `cash_movements` (livro-razão imutável) |
| **Obrigação / saldo com o cliente** | Saldo devedor de comanda aberta (`balance_due`), devolução financeira pendente de lançamento estornado (`customer_refund_status = 'pending'`) e, no Bloco 4, crédito antecipado | `tabs` + `sales.customer_refund_amount` + saldos derivados do livro-razão |

Regras de ouro:
- Venda/consumo é diferente de recebimento.
- Pagamento parcial de comanda **não** é uma nova venda: quita principal consumido e jamais duplica receita nos relatórios.
- Recebimento é diferente de crédito antecipado.
- Taxa repassada é diferente de valor dos produtos e incide **somente** sobre o principal pago no cartão.
- Troco em dinheiro **não** é despesa operacional nem venda adicional.
- Estorno **não** apaga o histórico original e **não** presume devolução automática do dinheiro ao cliente.
- Todos os valores monetários são `Decimal` no Python e `DECIMAL(10, 2)` no MariaDB (nunca `float`).
- Preços, saldos de comanda e totais são sempre calculados e validados no backend.

## 2. Tabelas implementadas (`003` e `004`)

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
tabs(
  id, shift_id, number INT UNSIGNED (1..999999), customer_name NULL,
  status ENUM('open', 'closed'),
  open_number INT UNSIGNED GENERATED ALWAYS AS (IF(status = 'open', number, NULL)) PERSISTENT UNIQUE,
  opened_by, opened_at, closed_by NULL, closed_at NULL
)
sales(
  id, shift_id, tab_id NULL, kind ENUM('quick', 'tab_item'), status ENUM('active', 'voided'),
  subtotal DECIMAL(10,2), card_fee_total DECIMAL(10,2), total_charged DECIMAL(10,2),
  operator_id, created_at, idempotency_key UNIQUE, request_fingerprint CHAR(64),
  voided_by, voided_at, void_reason,
  customer_refund_status ENUM('not_applicable', 'pending', 'completed'),
  customer_refund_amount DECIMAL(10,2) DEFAULT 0.00,
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
  reverses_id BIGINT UNSIGNED NULL,
  operator_id, created_at, idempotency_key UNIQUE,
  batch_key VARCHAR(64) NULL, request_fingerprint CHAR(64) NULL, note
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

## 6. Comandas abertas, pagamentos parciais e encerramento (Bloco 3)

### 6.1. Identificação e ciclo de vida
- Cada comanda possui identificador interno imutável (`tabs.id`) e número manual informado pelo operador (`tabs.number`, correspondente à ficha física).
- Unicidade entre comandas abertas é garantida no banco por coluna gerada persistida:
  - `open_number = IF(status = 'open', number, NULL)` com `UNIQUE (open_number)`.
  - Quando a comanda é encerrada (`status = 'closed'`), `open_number` passa a `NULL`, permitindo reutilizar o mesmo número em uma nova comanda com novo `id`, preservando todo o histórico anterior.

### 6.2. Consumo incremental (`sales.kind = 'tab_item'`)
- Cada lançamento de consumo em uma comanda aberta cria um registro em `sales` com `kind = 'tab_item'`, `tab_id = tabs.id`, `card_fee_total = 0.00` e `total_charged = subtotal`, acompanhado de seus itens em `sale_items`.
- Produtos cadastrados copiam `name` e `price` vigentes no momento do lançamento para `sale_items`; alterações posteriores no catálogo não modificam lançamentos passados.
- Nenhum movimento em `cash_movements` é gerado no momento do consumo.

### 6.3. Pagamentos parciais e divididos (`cash_movements.tab_id`)
- Pagamentos de comanda são gravados em `cash_movements` com `kind = 'sale_payment'`, `direction = 'in'`, `tab_id = tabs.id` e `sale_id = NULL`.
- Um pagamento pode ser parcial (quitando parte do saldo) ou dividido entre múltiplos métodos (`pix`, `card`, `cash`) no mesmo lote (`batch_key = idempotency_key`).
- Regra de bloqueio no Bloco 3:
  $$\sum \text{payments.amount} \le \text{balance\_due}$$
  Pagamentos cujo principal supere o saldo pendente da comanda são rejeitados (`principal_exceeds_balance`).
- A taxa de cartão incide **exclusivamente sobre a parcela de principal paga no cartão** naquele recebimento, usando a configuração vigente no ato do pagamento e gravando o snapshot (`fee_percent`, `fee_mode`, `fee_amount`, `gross_amount`).

### 6.4. Cálculo de saldo e identidade contábil unificada
Para cada comanda:
- $\text{consumed\_subtotal} = \sum \text{sales.subtotal}\ (\text{kind = 'tab\_item' AND status = 'active'})$
- $\text{paid\_principal\_net} = \sum \text{cash\_movements.amount}\ (\text{direction = 'in' AND kind = 'sale\_payment'}) - \sum \text{cash\_movements.amount}\ (\text{direction = 'out' AND kind = 'reversal'})$
- $\text{recognized\_paid\_principal} = \min(\text{consumed\_subtotal},\ \max(0.00,\ \text{paid\_principal\_net}))$
- $\text{balance\_due} = \max(0.00,\ \text{consumed\_subtotal} - \text{recognized\_paid\_principal})$

No resumo financeiro global do turno (`get_financial_summary`), não há duplicação entre consumo de comanda e pagamento de comanda:
$$\text{active\_sales\_subtotal} = \text{quick\_sales\_subtotal} + \text{tabs\_consumed\_subtotal}$$
$$\text{active\_sales\_subtotal} = \text{principal\_received\_total} + \text{tabs\_pending\_balance\_total}$$

**Condições de validade dessa igualdade:**
1. **Liquidação integral das vendas rápidas ativas:** toda venda rápida ativa possui soma de principal recebido igual ao seu `subtotal`; quando estornada (`status = 'voided'`), deixa de compor tanto `quick_sales_subtotal` quanto `recognized_principal` (esteja a devolução física `pending` ou `completed`).
2. **Teto de reconhecimento por comanda:** em cada comanda $t$, o principal contábil reconhecido como receita recebida limita-se ao consumo ativo ($\text{recognized\_paid\_principal}_t \le \text{consumed\_subtotal}_t$). Quando um lançamento pago é cancelado gerando excedente ($\text{paid\_principal\_net}_t > \text{consumed\_subtotal}_t$), esse excedente constitui passivo de devolução (`pending_customer_refunds_total`), sendo deduzido de `recognized_principal` até sua efetiva devolução.
3. **Encerramento apenas com saldo zero:** comandas encerradas (`status = 'closed'`) possuem obrigatoriamente `balance_due = 0.00` e `recognized_paid_principal = consumed_subtotal`.
4. **Isolamento estrito de créditos antecipados (Bloco 4):** a consulta de `recognized_principal` e o cálculo de `paid_principal_net` filtram exclusivamente `kind = 'sale_payment'` (entradas de quitação de consumo) e `kind = 'reversal'` (estornos de pagamento). Depósitos antecipados (`kind = 'credit_deposit'`) e devoluções de sobra de crédito (`kind = 'credit_refund'`) **nunca** entram em `active_sales_subtotal` nem em `principal_received_total` enquanto não consumidos.

### 6.5. Cancelamento de lançamento em comanda, pagamentos mistos e taxas de cartão
- O cancelamento de um lançamento (`POST /api/tabs/<tab_id>/launches/<sale_id>/void`) exige perfil `admin`, motivo obrigatório, turno aberto e comanda aberta.
- O lançamento recebe `status = 'voided'`, reduzindo `consumed_subtotal`.
- **Caso A — Pagamentos já realizados continuam $\le$ novo consumo ativo:**
  - Não há valor a devolver ao cliente; apenas o `balance_due` da comanda é reduzido.
  - `customer_refund_status = 'not_applicable'` e `customer_refund_amount = 0.00`.
- **Caso B — Pagamentos líquidos já realizados superam o novo consumo ativo (`excess_principal > 0.00`):**
  - **Estratégia de alocação em pagamentos mistos (`plan_tab_excess_reversals`):**
    1. O excedente de principal é alocado **primeiro sobre pagamentos sem taxa de operadora (`cash` e `pix`, em ordem LIFO)** e **somente depois sobre parcelas de `card` (em ordem LIFO)** caso o excedente supere todo o principal disponível em dinheiro/Pix.
    2. Isso garante determinismo em pagamentos divididos (o valor a devolver independe da ordem em que o operador digitou `[pix, card]` ou `[card, pix]`) e preserva intactas as transações de cartão sempre que os meios sem taxa absorverem o cancelamento.
  - **Distinção entre taxa cobrada do cliente e taxa retida pela operadora (sem presunção de estorno automático na maquininha):**
    1. O cancelamento de um item de consumo em comanda (`tab_item`) cancela consumo de produto (`subtotal`), e **não** cancela automaticamente a transação na maquininha física.
    2. Como a operadora do cartão (adquirente) retém sua taxa sobre a transação passada no terminal (especialmente em cancelamentos parciais de comanda ou quando a devolução ao cliente é feita em dinheiro/Pix no caixa), o sistema **não presume devolução automática nem proporcional da taxa de cartão pela maquininha**: `fee_to_reverse = 0.00` e `customer_refund_amount = excess_principal`.
    3. O resumo financeiro diferencia explicitamente:
       - `card_fees_charged_total`: total de taxas de cartão efetivamente cobradas nas transações de entrada (`direction = 'in'`), base sobre a qual incide a retenção da operadora;
       - `card_fees_reversed_total`: taxas de cartão revertidas apenas quando há estorno integral de transação de cartão (`direction = 'out'`);
       - `card_fees_received_total`: taxas líquidas reconhecidas nas operações ativas.
  - Se `customer_refund_completed = False` (padrão seguro), **nenhuma saída fictícia é gerada no caixa**: o lançamento fica com `customer_refund_status = 'pending'` e a comanda fica impedida de encerrar até a resolução.
  - Quando a devolução física ao cliente é confirmada (`POST /api/tabs/<tab_id>/launches/<sale_id>/customer-refund` ou `customer_refund_completed = True`), são inseridos os movimentos `kind = 'reversal'`, `direction = 'out'` em `cash_movements` (suportando reversão parcial ou integral de parcelas via índice `ix_cash_movements_reverses`).

### 6.6. Regras de encerramento (`POST /api/tabs/<tab_id>/close`)
Uma comanda só pode ser encerrada quando:
1. `status == 'open'`;
2. `balance_due == 0.00`;
3. `pending_refund_total == 0.00` (nenhuma devolução pendente ao cliente).

Após encerrada, a comanda torna-se estritamente somente leitura (bloqueia novos consumos, pagamentos, cancelamentos ou novo encerramento).

## 7. Transações, concorrência e idempotência

- Todas as operações de venda rápida e de comanda executam dentro de `MariaDbUnitOfWork`, bloqueando a linha da comanda (`SELECT ... FOR UPDATE`) para serializar operações concorrentes sobre a mesma comanda (impedindo dois pagamentos simultâneos que juntos ultrapassem o saldo ou adição de item simultânea ao encerramento).
- `idempotency_key` + `request_fingerprint` (SHA-256 canônico) são verificados em:
  - `sales` (venda rápida e lançamento de consumo em comanda);
  - `cash_movements.batch_key` (pagamento parcial ou dividido de comanda).
- Reenvio com mesmo payload retorna `200 OK` com `idempotent_replay = true`; reenvio da mesma chave com payload diferente retorna `409 Conflict` (`idempotency_conflict`).

## 8. Integração futura com Crédito Antecipado (Bloco 4) e Fechamento de Caixa (Bloco 5)

### 8.1. Preparação para Crédito Antecipado na Comanda (Bloco 4)
O esquema atual já contém os tipos de movimento `credit_deposit` e `credit_refund` no `ENUM` de `cash_movements.kind`. No Bloco 4:
1. Quando o cliente depositar um valor antecipado na comanda (ex.: coloca R$ 100,00 ao abrir a ficha), o recebimento será gravado em `cash_movements` com `tab_id = tabs.id`, `kind = 'credit_deposit'`, `direction = 'in'` (ou convertendo o excesso autorizado de pagamento em crédito da comanda).
2. O saldo da comanda passará a expor dois indicadores complementares derivados do mesmo livro-razão:
   - `balance_due = max(0, consumed_subtotal - net_paid_and_credited_principal)` (saldo devedor a pagar);
   - `credit_balance = max(0, net_paid_and_credited_principal - consumed_subtotal)` (crédito disponível a consumir ou devolver).
3. Enquanto houver `credit_balance > 0`, o encerramento da comanda exigirá ou o consumo desse saldo ou a devolução explícita do troco/sobra ao cliente via `cash_movements` (`kind = 'credit_refund'`, `direction = 'out'`), zerando a obrigação antes de fechar a comanda.

### 8.2. Impacto no Fechamento de Caixa (Bloco 5)
- Como vendas rápidas e comandas compartilham as tabelas `sales` e `cash_movements` vinculadas a `shift_id`, o fechamento de caixa no Bloco 5 consultará um único livro-razão:
  - **Entradas e saídas físicas por método (`net_gross`)**: refletem exatamente o dinheiro na gaveta (`opening_cash + by_method.cash.net_gross`) e os valores liquidados em Pix e Cartão, incluindo pagamentos parciais de comandas.
  - **Verificação de pendências de turno**: o fechamento poderá alertar ou bloquear o encerramento do turno caso existam comandas ainda abertas (`tabs_open_count > 0` ou `tabs_pending_balance_total > 0`) ou devoluções financeiras pendentes ao cliente (`pending_customer_refunds_count > 0`).
