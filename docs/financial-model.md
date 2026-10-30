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
2. `balance_due == 0.00` (nenhum saldo devedor);
3. `credit_balance == 0.00` (nenhum saldo de crédito disponível pendente de consumo ou devolução);
4. `pending_refund_total == 0.00` (nenhuma devolução pendente ao cliente).

O objeto `Tab` expõe `can_close: bool` e `close_blockers: list[str]` (`credit_balance_pending`, `balance_due_pending`, `customer_refund_pending`, `already_closed`) para orientar o operador. Após encerrada, a comanda torna-se estritamente somente leitura (bloqueia novos consumos, depósitos, devoluções, pagamentos, cancelamentos ou novo encerramento).

## 7. Transações, concorrência e idempotência

- Todas as operações de venda rápida e de comanda executam dentro de `MariaDbUnitOfWork`, bloqueando a linha da comanda (`SELECT ... FOR UPDATE`) para serializar operações concorrentes sobre a mesma comanda (impedindo dois pagamentos ou devoluções simultâneas que juntos ultrapassem o saldo ou adição de item simultânea ao encerramento).
- `idempotency_key` + `request_fingerprint` (SHA-256 canônico) são verificados em:
  - `sales` (venda rápida e lançamento de consumo em comanda);
  - `cash_movements.batch_key` / `cash_movements.idempotency_key` (pagamento parcial/dividido de comanda, depósito antecipado de crédito e devolução de saldo de crédito).
- Reenvio com mesmo payload retorna `200 OK` com `idempotent_replay = true`; reenvio da mesma chave com payload diferente retorna `409 Conflict` (`idempotency_conflict`).

## 8. Crédito Antecipado, Consumo e Devolução de Saldo (Bloco 4)

### 8.1. Distinção contábil e de caixa: Depósito × Consumo × Devolução
O Bloco 4 permite que um cliente deposite dinheiro antecipadamente em uma comanda aberta (`kind = 'credit_deposit'`, `direction = 'in'`), consuma produtos utilizando esse crédito e receba eventual saldo restante de volta (`kind = 'credit_refund'`, `direction = 'out'`), inclusive por método diferente do depósito original:

1. **Depósito antecipado (`POST /api/tabs/<tab_id>/credits`):**
   - Registra entrada financeira em `cash_movements` com `tab_id = tabs.id`, `sale_id = NULL`, `kind = 'credit_deposit'`, `direction = 'in'`.
   - É um **passivo de crédito perante o cliente**, e **não** uma venda de produtos (não altera `active_sales_subtotal`).
   - Aceita `pix`, `cash` (com `cash_tendered` e `change_given`) ou `card` (inclusive dividido).
   - **Taxa de cartão em depósitos:** incide apenas sobre a parcela paga no cartão usando a configuração vigente (`payment_settings`), é repassada ao cliente (`gross_amount = amount + fee_amount`) e **não aumenta o saldo de crédito disponível** (`credit_deposited` soma apenas o principal `amount`).
2. **Aplicação automática do crédito ao consumo (`POST /api/tabs/<tab_id>/items`):**
   - **Regra central:** consumir crédito **não** significa receber novamente o valor e **não cria um novo movimento em `cash_movements`**.
   - A aplicação é **automática e determinística** a partir do estado persistido da comanda: todo consumo ativo (`sales` com `kind = 'tab_item'` e `status = 'active'`) reduz automaticamente o saldo de crédito disponível até o limite do pool líquido de crédito da comanda.
   - Caso o consumo acumulado supere o crédito disponível, o excedente torna-se `balance_due` (saldo a pagar), que pode ser quitado parcial ou integralmente via `POST /api/tabs/<tab_id>/payments` (`kind = 'sale_payment'`).
3. **Devolução de saldo de crédito (`POST /api/tabs/<tab_id>/credit-refunds`):**
   - Permite devolver parcial ou integralmente o `credit_balance` disponível ao cliente por `pix`, `cash` ou `card`, mesmo que diferente do método do depósito original.
   - Registra saída financeira real em `cash_movements` com `tab_id = tabs.id`, `sale_id = NULL`, `kind = 'credit_refund'`, `direction = 'out'`, `fee_amount = 0.00`, `gross_amount = amount`.
   - Bloqueia qualquer devolução superior ao `credit_balance` atual (`409 Conflict` / `insufficient_credit`) sob bloqueio pessimista (`SELECT ... FOR UPDATE`).

### 8.2. Fórmulas de Saldo da Comanda e Invariantes de Reconciliação
Para cada comanda, sejam:
- $C = \text{subtotal\_consumed}$: soma dos `sales.subtotal` ativos (`kind = 'tab_item'`, `status = 'active'`);
- $P_{\text{ord}}$: principal líquido de pagamentos ordinários (`kind = 'sale_payment'` menos estornos `kind = 'reversal'` vinculados a esses pagamentos), limitado a $C$: $\text{principal\_paid} = \min(C, P_{\text{ord}})$;
- $D_{\text{net}} = \text{credit\_deposited} = \max(0, D_{\text{in}} - D_{\text{rev}})$: principal líquido de depósitos de crédito (`kind = 'credit_deposit'` menos estornos administrativos `kind = 'reversal'` vinculados a depósitos);
- $R_{\text{credit}} = \text{credit\_refunded}$: soma das devoluções de saldo de crédito (`kind = 'credit_refund'`, `direction = 'out'`);
- $K_{\text{pool}} = \max(0, D_{\text{net}} - R_{\text{credit}})$: pool de crédito líquido disponível para cobrir consumo na comanda.

Então os saldos derivados da comanda são:
$$\text{credit\_applied} = \min(C - \text{principal\_paid},\ K_{\text{pool}})$$
$$\text{credit\_balance} = \max(0,\ K_{\text{pool}} - \text{credit\_applied})$$
$$\text{settled\_subtotal} = \text{principal\_paid} + \text{credit\_applied}$$
$$\text{balance\_due} = \max(0,\ C - \text{settled\_subtotal})$$

Essas definições garantem duas identidades globais exatas no resumo financeiro (`GET /api/sales/summary`):

1. **Identidade Global das Vendas Ativas:**
   $$\text{active\_sales\_subtotal} = \text{quick\_sales\_subtotal} + \text{tabs\_consumed\_subtotal} = \text{principal\_received\_total} + \text{tabs\_credit\_applied\_total} + \text{tabs\_pending\_balance\_total}$$
2. **Identidade Global do Passivo de Crédito Antecipado:**
   $$\text{tabs\_credit\_deposited\_total} = \text{tabs\_credit\_applied\_total} + \text{tabs\_credit\_refunded\_total} + \text{tabs\_credit\_balance\_total}$$

### 8.3. Exemplo Completo de Reconciliação (Comanda 15)
1. Abertura da Comanda 15 (`credit_balance = 0.00`, `balance_due = 0.00`).
2. Depósito antecipado de R$ 50,00 via Pix (`kind = 'credit_deposit'`, `direction = 'in'`, `method = 'pix'`, `amount = 50.00`):
   - `credit_deposited = 50.00`, `credit_balance = 50.00`, `active_sales_subtotal = 0.00`.
3. Consumo de R$ 20,00 + Consumo de R$ 12,00 (`subtotal_consumed = 32.00`):
   - `credit_applied = 32.00`, `credit_balance = 18.00`, `balance_due = 0.00`, `active_sales_subtotal = 32.00` (nenhuma nova entrada de caixa é criada).
4. Devolução do saldo restante de R$ 18,00 em dinheiro (`kind = 'credit_refund'`, `direction = 'out'`, `method = 'cash'`, `amount = 18.00`):
   - `credit_refunded = 18.00`, `credit_balance = 0.00`, `balance_due = 0.00`, `can_close = True`.
5. Encerramento da Comanda 15 e reconciliação por método:
   - Produtos vendidos (`active_sales_subtotal`): **R$ 32,00**
   - Pix (`in_gross` / `out_gross` / `net_gross`): **+R$ 50,00 / R$ 0,00 / +R$ 50,00**
   - Dinheiro (`in_gross` / `out_gross` / `net_gross`): **R$ 0,00 / -R$ 18,00 / -R$ 18,00**
   - Recebimento líquido total em caixa (`net_received_total`): **R$ 32,00**
   - Saldo de crédito pendente (`tabs_credit_balance_total`): **R$ 0,00**
   - Saldo devedor pendente (`tabs_pending_balance_total`): **R$ 0,00**

### 8.4. Cancelamentos e Estornos com Crédito Antecipado
- **Estorno administrativo de depósito (`POST /api/tabs/<tab_id>/credits/<movement_id>/void`, somente Admin):**
  - Exige motivo obrigatório, comanda aberta e turno aberto.
  - Verifica se o cancelamento não deixaria o pool líquido de depósitos menor que o total já devolvido via `credit_refund`.
  - **Separação entre cancelamento contábil e saída financeira física**:
    - Quando `customer_refund_completed = false` (padrão), o depósito é cancelado contabilmente no próprio registro (`voided_by`, `voided_at`, `void_reason`, `customer_refund_status = 'pending'`, `customer_refund_amount = gross_amount`). O crédito disponível da comanda (`credit_deposited`) é reduzido imediatamente e a obrigação de devolução fica registrada em `pending_refund_total` e `pending_customer_refunds_total` (bloqueando o encerramento da comanda), **sem inserir movimento `direction = 'out'` e sem reduzir artificialmente o caixa físico (`net_gross`) antes da devolução efetiva**.
    - Quando a devolução financeira ocorre no ato (`customer_refund_completed = true`) ou é confirmada posteriormente via `POST /api/tabs/<tab_id>/credits/<movement_id>/customer-refund`, o status passa para `completed` e o sistema insere o movimento `kind = 'reversal'`, `direction = 'out'`, `reverses_id = movement_id` (revertendo também a taxa de cartão apenas se o estorno ocorrer na maquininha), sem reduzir `credit_deposited` uma segunda vez.
  - Recalcula imediatamente `credit_deposited`, `credit_applied`, `credit_balance` e `balance_due` (se parte do depósito cancelado já cobria consumo, a diferença passa automaticamente para `balance_due`).
- **Cancelamento de consumo pago com crédito (`POST /api/tabs/<tab_id>/items/<sale_id>/void`, somente Admin):**
  - Quando o consumo cancelado estava coberto por crédito antecipado, a redução de `subtotal_consumed` reduz `credit_applied` e **restaura automaticamente o crédito em `credit_balance`**, sem gerar movimentos fictícios de entrada ou saída em `cash_movements` (`customer_refund_status = 'not_applicable'`).
  - Quando o consumo cancelado estava parcialmente coberto por crédito e parcialmente pago por pagamento complementar (`sale_payment`), o crédito é restaurado ao `credit_balance` e apenas eventual excesso de `sale_payment` sobre o consumo remanescente gera devolução financeira ao cliente (`pending` ou `completed`).

## 9. Impacto no Fechamento de Caixa (Bloco 5)
- Como vendas rápidas, comandas, depósitos antecipados e devoluções compartilham as tabelas `sales` e `cash_movements` vinculadas a `shift_id`, o fechamento de caixa no Bloco 5 consultará um único livro-razão:
  - **Entradas, saídas e líquido por método (`in_gross`, `out_gross`, `net_gross`)**: refletem exatamente o dinheiro físico na gaveta (`opening_cash + by_method.cash.net_gross`, já descontadas devoluções em espécie de crédito ou estornos) e os valores liquidados em Pix e Cartão.
  - **Verificação de pendências de turno**: o fechamento poderá inspecionar `tabs_open_count`, `tabs_pending_balance_total`, `tabs_credit_balance_total` e `pending_customer_refunds_count`.
