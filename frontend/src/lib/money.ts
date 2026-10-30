/**
 * Tipos e utilitários monetários no frontend (aritmética inteira em centavos
 * para exibição; a validação e o cálculo definitivos ocorrem sempre no backend).
 */

export type PaymentMethod = "pix" | "card" | "cash";
export type CardFeeMode = "simple_addition" | "pass_through";

export interface Product {
  id: number;
  name: string;
  category: string;
  price: string;
  is_active: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface PaymentSettings {
  card_fee_percent: string;
  card_fee_mode: CardFeeMode;
  updated_by?: number | null;
  updated_at?: string | null;
}

export interface SaleItemRecord {
  id: number;
  product_id: number | null;
  item_kind: "product" | "custom";
  description: string;
  quantity: number;
  unit_price: string;
  line_total: string;
}

export interface CashMovementRecord {
  id: number;
  kind: "sale_payment" | "credit_deposit" | "credit_refund" | "reversal";
  method: PaymentMethod;
  direction: "in" | "out";
  amount: string;
  fee_percent: string;
  fee_mode: string;
  fee_amount: string;
  gross_amount: string;
  cash_tendered: string | null;
  change_given: string | null;
  reverses_id: number | null;
  created_at: string;
  note?: string | null;
}

export interface SaleRecord {
  id: number;
  shift_id: number;
  kind: "quick" | "tab_item";
  status: "active" | "voided";
  subtotal: string;
  card_fee_total: string;
  total_charged: string;
  operator_id: number;
  created_at: string;
  idempotency_key: string;
  voided_by: number | null;
  voided_at: string | null;
  void_reason: string | null;
  customer_refund_status: "not_applicable" | "pending" | "completed";
  customer_refunded_at: string | null;
  customer_refunded_by: number | null;
  customer_refund_method: PaymentMethod | null;
  customer_refund_notes: string | null;
  items: SaleItemRecord[];
  payments: CashMovementRecord[];
}

export interface FinancialSummary {
  active_sales_count: number;
  voided_sales_count: number;
  pending_customer_refunds_count: number;
  active_sales_subtotal: string;
  active_card_fees: string;
  active_total_charged: string;
  voided_total_charged: string;
  pending_customer_refunds_total: string;
  completed_customer_refunds_total: string;
  by_method: Record<
    PaymentMethod,
    {
      recognized_principal: string;
      recognized_fee: string;
      recognized_gross: string;
      net_principal: string;
      net_fee: string;
      net_gross: string;
    }
  >;
}

/** Converte texto decimal ("18.50" ou "18,50") para centavos inteiros. Retorna null se inválido. */
export function parseToCents(raw: string): number | null {
  const cleaned = raw.trim().replace(",", ".");
  if (!/^\d+(\.\d{1,2})?$/.test(cleaned)) return null;
  const [intPart, decPart = ""] = cleaned.split(".");
  const paddedDec = (decPart + "00").slice(0, 2);
  const cents = Number(intPart) * 100 + Number(paddedDec);
  if (!Number.isSafeInteger(cents) || cents < 0) return null;
  return cents;
}

/** Formata centavos inteiros para string decimal da API ("18.50"). */
export function centsToApiDecimal(cents: number): string {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(Math.round(cents));
  const reais = Math.floor(abs / 100);
  const centavos = String(abs % 100).padStart(2, "0");
  return `${sign}${reais}.${centavos}`;
}

/** Formata centavos ou string decimal em moeda BRL ("R$ 18,50"). */
export function formatBRL(value: number | string): string {
  const cents = typeof value === "number" ? value : (parseToCents(value) ?? 0);
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(Math.round(cents));
  const reais = Math.floor(abs / 100).toLocaleString("pt-BR");
  const centavos = String(abs % 100).padStart(2, "0");
  return `${sign}R$ ${reais},${centavos}`;
}

/**
 * Calcula a taxa do cartão em centavos sobre uma parcela de principal (em centavos),
 * espelhando exatamente o ROUND_HALF_UP do backend.
 */
export function calculateCardFeeCents(
  principalCents: number,
  feePercentStr: string,
  feeMode: CardFeeMode,
): number {
  const pctBps = parseToCents(feePercentStr); // ex.: "3.00" -> 300 basis points
  if (!pctBps || pctBps <= 0 || principalCents <= 0) return 0;

  if (feeMode === "simple_addition") {
    // principalCents * (pctBps / 10000) com half-up
    return Math.floor((principalCents * pctBps + 5000) / 10000);
  }
  if (feeMode === "pass_through") {
    const denom = 10000 - pctBps;
    if (denom <= 0) return 0;
    const grossCents = Math.floor((principalCents * 10000 + Math.floor(denom / 2)) / denom);
    return grossCents - principalCents;
  }
  return 0;
}

export function methodLabel(method: PaymentMethod): string {
  switch (method) {
    case "pix":
      return "Pix";
    case "card":
      return "Cartão";
    case "cash":
      return "Dinheiro";
  }
}

export function generateIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `mx-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}
