# Mandaxpress — Modelo financeiro revisado (proposta para o Bloco 2+)

Status: **proposta, nada disso foi criado no banco.** Aguarda revisão antes do Bloco 2.

## 1. Princípio: três coisas separadas

| Conceito | O que é | Onde vive |
|---|---|---|
| **Venda / consumo** | Produtos ou valor avulso entregues ao cliente (receita) | `sales` + `sale_items` |
| **Movimento de caixa** | Dinheiro que entrou ou saiu de fato, por meio (pix/card/cash) | `cash_movements` (livro-razão imutável) |
| **Obrigação com o cliente** | Crédito antecipado ainda não consumido; saldo a pagar de comanda | **derivado** (nunca gravado) |

Regras de ouro: venda ≠ recebimento; depósito de crédito **não** é venda;
saldos são sempre calculados a partir de registros; nada é apagado ou editado.

## 2. Tabelas

```text
products(id, name, price DECIMAL(10,2), active, ...)
tabs(id, number, customer_name, status[open|closed], opened_by, opened_at, closed_by, closed_at)
shifts(id, opened_by, opened_at, opening_cash, closed_by, closed_at, counted_cash, notes)   -- turno de caixa
sales(id, shift_id, tab_id NULL, kind[quick|tab_item], status[active|voided], total DECIMAL,
      operator_id, created_at, idempotency_key UNIQUE, voided_by, voided_at, void_reason)
sale_items(id, sale_id, product_id NULL, description, quantity>0, unit_price>=0, line_total)
cash_movements(id, shift_id, tab_id NULL, sale_id NULL, kind, method[pix|card|cash],
      amount>0, direction[in|out], cash_tendered NULL, change_given NULL,
      reverses_id NULL UNIQUE, operator_id, created_at, idempotency_key UNIQUE, note)
credit_applications(id, tab_id, sale_id, amount>0, status[active|reversed], reverses_id NULL UNIQUE, ...)
audit_log(id, operator_id, action, entity, entity_id, reason, created_at)
```

`cash_movements.kind`: `sale_payment` (pagamento de venda/comanda), `credit_deposit`
(depósito antecipado), `credit_refund` (devolução do crédito restante),
`reversal` (estorno de outro movimento; aponta `reverses_id`).

`direction` e `kind` são consistentes por `CHECK`
(`sale_payment`/`credit_deposit` = `in`; `credit_refund` = `out`; `reversal` = inverso do original).

### Dinheiro entregue e troco

Em pagamento em **dinheiro**: `cash_tendered` (entregue) e `change_given` (troco),
com `amount = cash_tendered - change_given` e `CHECK (change_given = 0 OR method='cash')`.
`amount` é sempre o valor **líquido** que o negócio reconhece.

## 3. Exemplos que devem fechar exatamente

**Venda rápida de R$ 32 em dinheiro, cliente entrega R$ 50.**
`sales.total=32`; movimento `sale_payment cash in amount=32 tendered=50 change=18`.
Entrada líquida em dinheiro = **32** (nem 50, nem 14).

**Crédito: depósito Pix 50, consumo 32, devolução 18 em dinheiro.**
- `credit_deposit pix in 50`
- venda de 32 na comanda + `credit_application 32`
- `credit_refund cash out 18`

| Métrica | Valor |
|---|---|
| Produtos vendidos | 32 |
| Entrada Pix | 50 |
| Saída dinheiro | 18 |
| Recebimento líquido total | 32 |
| Crédito restante | 50 − 32 − 18 = **0** |

A separação por meio é preservada: Pix +50 e dinheiro −18 (o caixa físico baixa 18 mesmo sem ter recebido dinheiro).

**Comanda: consumo 40, paga 15 Pix, consumo 20, paga 45 dinheiro.**
Vendido 60; Pix 15; dinheiro 45; saldo a pagar 0.

## 4. Fórmulas (todas derivadas, em uma única consulta transacional)

```text
consumido(tab)        = Σ sales.total            (status=active)
crédito_aplicado(tab) = Σ credit_applications    (status=active)
depositado(tab)       = Σ credit_deposit.amount (líquido de estornos)
devolvido(tab)        = Σ credit_refund.amount  (líquido de estornos)
crédito_disponível    = depositado − devolvido − crédito_aplicado      (>= 0)
pago(tab)             = Σ sale_payment.amount   (líquido de estornos)
saldo_a_pagar         = consumido − crédito_aplicado − pago            (>= 0 ao encerrar = 0)
```

Ordem ao lançar consumo em comanda com crédito: aplica
`min(crédito_disponível, valor)` automaticamente; o excedente vira saldo a pagar,
quitável à parte (`sale_payment`). Não existe crédito negativo (ver §6).

**Encerrar comanda** exige `saldo_a_pagar = 0` **e** `crédito_disponível = 0`
(sobra deve ser devolvida via `credit_refund`, informando o meio).

**Fechamento do turno**
- Vendido = Σ vendas ativas (produtos × avulso separados por `product_id IS NULL`).
- Recebido por meio = Σ `in` − Σ `out` (líquido) por `method`.
- Crédito não consumido = Σ `crédito_disponível` das comandas abertas.
- Pendente = Σ `saldo_a_pagar` das comandas abertas.
- Devolvido = Σ `credit_refund`.
- Dinheiro esperado em caixa = `opening_cash` + líquido em `cash`; conferido contra `counted_cash`.

Recebimento líquido do dia **não** soma depósitos como venda: depósito entra em "recebido",
só vira "vendido" quando consumido.

## 5. Estornos (sempre por novo registro, com motivo e operador)

| Situação | Regra |
|---|---|
| **Cancelar venda rápida** | `sales.status=voided`. Os pagamentos dela recebem `reversal` (saída) pelo mesmo meio. Admin ou operador no turno aberto, com motivo. |
| **Cancelar lançamento em comanda** | `voided`; se havia `credit_application` ligada, ela é `reversed` (o crédito volta a ficar disponível); se isso deixa a comanda **paga a mais**, o excedente não vira crédito: exige `credit_refund`/`reversal` explícito antes de permitir o cancelamento. |
| **Estornar pagamento** | `reversal` (inverso, mesmo meio e valor). Só é permitido se ainda não estornado (`reverses_id UNIQUE`). Volta a aumentar o saldo a pagar. |
| **Estornar depósito antecipado** | `reversal` (saída). Só permitido se `crédito_disponível >= valor`; se parte já foi consumida, primeiro cancela-se o consumo ou cobra-se a diferença. |
| **Reverter crédito aplicado** | `credit_application` → `reversed` (novo registro `reverses_id`); crédito disponível sobe, saldo a pagar sobe na mesma medida. Nunca altera o preço da venda. |
| **Devolução do crédito restante** | `credit_refund` ≤ `crédito_disponível`; meio livre (pode diferir do depósito). |

Nenhum estorno gera crédito fictício: toda operação é validada contra os saldos
derivados **dentro da transação, com a comanda travada** (`SELECT ... FOR UPDATE`),
de modo que nenhum saldo derivado fique negativo. Estorno de estorno é proibido.
Toda correção grava `audit_log` (quem, quando, por quê). Estornos acontecem
no turno em que são feitos (a data do movimento é a do estorno), mantendo turnos anteriores intactos.

## 6. Como cada invariante é garantido

| Invariante | Banco | Domínio / aplicação |
|---|---|---|
| Decimal exato | `DECIMAL(10,2)`; sem `FLOAT`/`DOUBLE` | Classe `Money` sobre `Decimal`; parse de string; rejeita float e > 2 casas; arredondamento explícito |
| Atomicidade | Transação InnoDB por operação (a `UnitOfWork` do Bloco 1 já faz commit/rollback) | Um caso de uso = uma UoW; efeitos (venda + itens + pagamento) juntos |
| Idempotência | `UNIQUE(idempotency_key)` em `sales` e `cash_movements` | Cliente gera UUID por intenção; reenvio devolve o resultado original; chave reutilizada com corpo diferente → 409 |
| Saldos derivados | Sem coluna `balance`; views/consultas sobre registros | Funções puras testadas (`credit_available`, `balance_due`) |
| Histórico preservado | Sem `DELETE`/`UPDATE` de valores; o usuário da app sem `DELETE` nas tabelas financeiras; triggers `BEFORE UPDATE/DELETE` bloqueando alteração de campos monetários | Correção só por `voided`/`reversal`/`reversed` |
| Concorrência | `SELECT ... FOR UPDATE` na linha de `tabs` (e `shifts`) antes de calcular saldo | Validar saldo **depois** do lock, na mesma transação |
| Preços históricos | `sale_items.unit_price` copiado no ato | Preço lido do banco no servidor; o cliente nunca envia preço/total |
| Fechamento por movimentos efetivos | Consultas só em `cash_movements` (líquido) e `sales` ativas | Relatório não lê nenhum campo "total" cacheado |
| Vendas ≠ recebimentos ≠ obrigações | Tabelas distintas; `kind` com `CHECK` | Nomes e tipos separados; depósito nunca entra em "vendido" |
| Valores sãos | `CHECK (amount > 0)`, `quantity > 0`, `unit_price >= 0`, `change_given <= cash_tendered` | Validação duplicada no domínio e mensagens claras |
| Comanda encerrada imutável | Trigger rejeita inserts em `tabs` fechadas (ou checagem sob lock) | Caso de uso verifica status sob lock |

Observação: o MariaDB aceita `CHECK` e triggers; vale validar em teste de integração
que as constraints realmente rejeitam (não confiar só na documentação).

## 7. Decisões que ainda preciso de você

1. Permissão de estorno: só admin, ou operador também (com motivo)?
2. Pagamento dividido: só em venda rápida, ou também em comanda?
3. Venda rápida paga **na hora** sempre: confirmar que não existe "fiado" fora da comanda.
4. Estorno em turno já fechado: proibir (recomendado) ou permitir com ajuste no turno atual?
5. Taxa de cartão e Pix: ignorar (valor bruto), certo?
