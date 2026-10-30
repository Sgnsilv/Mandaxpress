"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, apiFetch } from "@/lib/api";
import {
  PaymentMethod,
  PaymentSettings,
  Product,
  SaleRecord,
  TabRecord,
  calculateCardFeeCents,
  centsToApiDecimal,
  formatBRL,
  generateIdempotencyKey,
  methodLabel,
  parseToCents,
} from "@/lib/money";

interface CartItem {
  key: string;
  kind: "product" | "custom";
  productId?: number;
  description: string;
  unitPriceCents: number;
  quantity: number;
}

interface PaymentDraft {
  id: string;
  method: PaymentMethod;
  amountInput: string;
  cashTenderedInput: string;
}

interface TabsManagerProps {
  tabs: TabRecord[];
  products: Product[];
  settings: PaymentSettings;
  isAdmin: boolean;
  onDataChanged: () => Promise<void>;
}

export function TabsManager({
  tabs,
  products,
  settings,
  isAdmin,
  onDataChanged,
}: TabsManagerProps) {
  // Filtros e seleção de comanda
  const [statusFilter, setStatusFilter] = useState<"open" | "closed" | "all">("open");
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedTabId, setSelectedTabId] = useState<number | null>(null);

  // Formulário rápido de abertura de comanda
  const [newTabNumber, setNewTabNumber] = useState("");
  const [newTabCustomer, setNewTabCustomer] = useState("");
  const [openingTab, setOpeningTab] = useState(false);
  const [openTabError, setOpenTabError] = useState<string | null>(null);

  // Modo de operação dentro da comanda aberta: lançar consumo vs registrar pagamento
  const [actionMode, setActionMode] = useState<"consume" | "pay">("consume");
  const [feedbackMsg, setFeedbackMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);

  // Rascunho de novo lançamento de consumo
  const [prodSearch, setProdSearch] = useState("");
  const [prodCategory, setProdCategory] = useState("all");
  const [launchCart, setLaunchCart] = useState<CartItem[]>([]);
  const [customDesc, setCustomDesc] = useState("");
  const [customAmount, setCustomAmount] = useState("");
  const [customQty, setCustomQty] = useState("1");
  const [launchIdemKey, setLaunchIdemKey] = useState<string>(() => generateIdempotencyKey());
  const [submittingLaunch, setSubmittingLaunch] = useState(false);

  // Rascunho de pagamento parcial/integral na comanda
  const [paymentDrafts, setPaymentDrafts] = useState<PaymentDraft[]>([
    { id: "tpay-1", method: "pix", amountInput: "", cashTenderedInput: "" },
  ]);
  const [paymentNote, setPaymentNote] = useState("");
  const [payIdemKey, setPayIdemKey] = useState<string>(() => generateIdempotencyKey());
  const [submittingPayment, setSubmittingPayment] = useState(false);

  // Modal de confirmação de encerramento da comanda
  const [closeModalOpen, setCloseModalOpen] = useState(false);
  const [closeNotes, setCloseNotes] = useState("");
  const [submittingClose, setSubmittingClose] = useState(false);

  // Modal de cancelamento auditável de lançamento (admin)
  const [voidLaunchTarget, setVoidLaunchTarget] = useState<SaleRecord | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [voidRefundCompleted, setVoidRefundCompleted] = useState(false);
  const [voidRefundMethod, setVoidRefundMethod] = useState<PaymentMethod>("pix");
  const [voidRefundNotes, setVoidRefundNotes] = useState("");
  const [submittingVoid, setSubmittingVoid] = useState(false);
  const [voidError, setVoidError] = useState<string | null>(null);

  // Modal de confirmação de devolução efetiva de lançamento cancelado (admin)
  const [refundLaunchTarget, setRefundLaunchTarget] = useState<SaleRecord | null>(null);
  const [refundMethod, setRefundMethod] = useState<PaymentMethod>("pix");
  const [refundNotes, setRefundNotes] = useState("");
  const [submittingRefund, setSubmittingRefund] = useState(false);
  const [refundError, setRefundError] = useState<string | null>(null);

  const openTabsCount = useMemo(() => tabs.filter((t) => t.status === "open").length, [tabs]);
  const closedTabsCount = useMemo(() => tabs.filter((t) => t.status === "closed").length, [tabs]);

  const filteredTabs = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return tabs.filter((t) => {
      if (statusFilter !== "all" && t.status !== statusFilter) return false;
      if (!q) return true;
      const numMatch = String(t.number) === q || String(t.number).includes(q);
      const nameMatch = (t.customer_name ?? "").toLowerCase().includes(q);
      return numMatch || nameMatch;
    });
  }, [tabs, statusFilter, searchQuery]);

  const selectedTab = useMemo(
    () => tabs.find((t) => t.id === selectedTabId) ?? null,
    [tabs, selectedTabId],
  );

  // Seleciona automaticamente a primeira comanda aberta se nenhuma estiver selecionada
  useEffect(() => {
    if (selectedTabId === null && filteredTabs.length > 0) {
      setSelectedTabId(filteredTabs[0].id);
    }
  }, [selectedTabId, filteredTabs]);

  const balanceDueCents = useMemo(
    () => (selectedTab ? (parseToCents(selectedTab.balance_due) ?? 0) : 0),
    [selectedTab],
  );

  const selectTab = useCallback((tab: TabRecord) => {
    setSelectedTabId(tab.id);
    setFeedbackMsg(null);
    setLaunchCart([]);
    setLaunchIdemKey(generateIdempotencyKey());
    const balCents = parseToCents(tab.balance_due) ?? 0;
    setPaymentDrafts([
      {
        id: "tpay-1",
        method: "pix",
        amountInput: balCents > 0 ? centsToApiDecimal(balCents) : "",
        cashTenderedInput: "",
      },
    ]);
    setPayIdemKey(generateIdempotencyKey());
  }, []);

  const activeProducts = useMemo(() => products.filter((p) => p.is_active), [products]);

  const categories = useMemo(() => {
    const s = new Set<string>();
    for (const p of activeProducts) s.add(p.category);
    return Array.from(s).sort();
  }, [activeProducts]);

  const filteredProducts = useMemo(() => {
    const q = prodSearch.trim().toLowerCase();
    return activeProducts.filter((p) => {
      if (prodCategory !== "all" && p.category !== prodCategory) return false;
      if (q && !p.name.toLowerCase().includes(q) && !p.category.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [activeProducts, prodSearch, prodCategory]);

  // Cálculos do rascunho de lançamento
  const launchSubtotalCents = useMemo(
    () => launchCart.reduce((acc, it) => acc + it.unitPriceCents * it.quantity, 0),
    [launchCart],
  );

  // Cálculos do rascunho de pagamento na comanda
  const computedPayments = useMemo(() => {
    return paymentDrafts.map((p) => {
      const amountCents = parseToCents(p.amountInput) ?? 0;
      const feeCents =
        p.method === "card"
          ? calculateCardFeeCents(amountCents, settings.card_fee_percent, settings.card_fee_mode)
          : 0;
      const grossCents = amountCents + feeCents;
      const tenderedCents =
        p.method === "cash"
          ? p.cashTenderedInput.trim()
            ? (parseToCents(p.cashTenderedInput) ?? 0)
            : amountCents
          : null;
      const changeCents =
        p.method === "cash" && tenderedCents !== null ? tenderedCents - grossCents : null;
      return {
        ...p,
        amountCents,
        feeCents,
        grossCents,
        tenderedCents,
        changeCents,
      };
    });
  }, [paymentDrafts, settings]);

  const paymentPrincipalSumCents = useMemo(
    () => computedPayments.reduce((acc, p) => acc + p.amountCents, 0),
    [computedPayments],
  );
  const paymentFeeSumCents = useMemo(
    () => computedPayments.reduce((acc, p) => acc + p.feeCents, 0),
    [computedPayments],
  );
  const paymentGrossSumCents = paymentPrincipalSumCents + paymentFeeSumCents;
  const remainingBalanceAfterPayCents = balanceDueCents - paymentPrincipalSumCents;
  const paymentChangeSumCents = useMemo(
    () =>
      computedPayments.reduce(
        (acc, p) => acc + (p.changeCents !== null && p.changeCents > 0 ? p.changeCents : 0),
        0,
      ),
    [computedPayments],
  );

  async function handleOpenTab(e: React.FormEvent) {
    e.preventDefault();
    if (openingTab) return;
    setOpenTabError(null);
    setFeedbackMsg(null);

    const trimmedNum = newTabNumber.trim();
    if (!/^\d+$/.test(trimmedNum) || Number(trimmedNum) < 1) {
      setOpenTabError("Informe um número inteiro válido para a comanda (ex.: 12).");
      return;
    }

    setOpeningTab(true);
    try {
      const resp = await apiFetch<{ tab: TabRecord }>("/tabs", {
        method: "POST",
        body: JSON.stringify({
          number: Number(trimmedNum),
          customer_name: newTabCustomer.trim() || undefined,
        }),
      });
      setNewTabNumber("");
      setNewTabCustomer("");
      setStatusFilter("open");
      await onDataChanged();
      selectTab(resp.tab);
      setActionMode("consume");
      setFeedbackMsg({
        type: "ok",
        text: `Comanda #${resp.tab.number} aberta com sucesso.`,
      });
    } catch (err) {
      setOpenTabError(err instanceof ApiError ? err.message : "Erro ao abrir comanda.");
    } finally {
      setOpeningTab(false);
    }
  }

  function addProductToLaunch(product: Product) {
    const unitPriceCents = parseToCents(product.price) ?? 0;
    setFeedbackMsg(null);
    setLaunchIdemKey(generateIdempotencyKey());
    setLaunchCart((prev) => {
      const next = prev.map((it) => ({ ...it }));
      const idx = next.findIndex((it) => it.kind === "product" && it.productId === product.id);
      if (idx >= 0) {
        next[idx].quantity += 1;
      } else {
        next.push({
          key: `lprod-${product.id}`,
          kind: "product",
          productId: product.id,
          description: product.name,
          unitPriceCents,
          quantity: 1,
        });
      }
      return next;
    });
  }

  function updateLaunchQty(key: string, delta: number) {
    setFeedbackMsg(null);
    setLaunchIdemKey(generateIdempotencyKey());
    setLaunchCart((prev) => {
      const next: CartItem[] = [];
      for (const it of prev) {
        if (it.key !== key) {
          next.push(it);
          continue;
        }
        const nq = it.quantity + delta;
        if (nq >= 1) next.push({ ...it, quantity: nq });
      }
      return next;
    });
  }

  function addCustomItemToLaunch(e: React.FormEvent) {
    e.preventDefault();
    setFeedbackMsg(null);
    const desc = customDesc.trim();
    const amountCents = parseToCents(customAmount);
    const qty = Number(customQty);
    if (desc.length < 2) {
      setFeedbackMsg({ type: "err", text: "Informe uma descrição válida (mínimo 2 caracteres)." });
      return;
    }
    if (amountCents === null || amountCents <= 0) {
      setFeedbackMsg({ type: "err", text: "Informe um valor unitário maior que R$ 0,00." });
      return;
    }
    if (!Number.isInteger(qty) || qty < 1 || qty > 1000) {
      setFeedbackMsg({ type: "err", text: "Informe uma quantidade entre 1 e 1000." });
      return;
    }

    setLaunchIdemKey(generateIdempotencyKey());
    setLaunchCart((prev) => [
      ...prev,
      {
        key: `lcustom-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
        kind: "custom",
        description: desc,
        unitPriceCents: amountCents,
        quantity: qty,
      },
    ]);
    setCustomDesc("");
    setCustomAmount("");
    setCustomQty("1");
  }

  async function handleConfirmLaunch() {
    if (!selectedTab || submittingLaunch || launchCart.length === 0) return;
    setSubmittingLaunch(true);
    setFeedbackMsg(null);
    try {
      const payload = {
        idempotency_key: launchIdemKey,
        items: launchCart.map((it) =>
          it.kind === "product"
            ? { product_id: it.productId, quantity: it.quantity }
            : {
                custom_description: it.description,
                custom_amount: centsToApiDecimal(it.unitPriceCents),
                quantity: it.quantity,
              },
        ),
      };
      const resp = await apiFetch<{ tab: TabRecord; launch: SaleRecord; idempotent_replay: boolean }>(
        `/tabs/${selectedTab.id}/items`,
        {
          method: "POST",
          body: JSON.stringify(payload),
        },
      );
      setLaunchCart([]);
      setLaunchIdemKey(generateIdempotencyKey());
      const newBalCents = parseToCents(resp.tab.balance_due) ?? 0;
      setPaymentDrafts([
        {
          id: "tpay-1",
          method: "pix",
          amountInput: newBalCents > 0 ? centsToApiDecimal(newBalCents) : "",
          cashTenderedInput: "",
        },
      ]);
      setPayIdemKey(generateIdempotencyKey());
      await onDataChanged();
      setFeedbackMsg({
        type: "ok",
        text: `Lançamento de ${formatBRL(resp.launch.subtotal)} registrado na Comanda #${resp.tab.number}. Novo saldo pendente: ${formatBRL(resp.tab.balance_due)}.`,
      });
    } catch (err) {
      setFeedbackMsg({
        type: "err",
        text: err instanceof ApiError ? err.message : "Erro ao registrar lançamento na comanda.",
      });
    } finally {
      setSubmittingLaunch(false);
    }
  }

  function addPaymentDraftLine() {
    setPayIdemKey(generateIdempotencyKey());
    const rest = Math.max(0, balanceDueCents - paymentPrincipalSumCents);
    setPaymentDrafts((prev) => [
      ...prev,
      {
        id: `tpay-${Date.now()}-${prev.length + 1}`,
        method: "cash",
        amountInput: rest > 0 ? centsToApiDecimal(rest) : "",
        cashTenderedInput: "",
      },
    ]);
  }

  function removePaymentDraftLine(id: string) {
    setPayIdemKey(generateIdempotencyKey());
    setPaymentDrafts((prev) => (prev.length <= 1 ? prev : prev.filter((p) => p.id !== id)));
  }

  function updatePaymentDraftLine(id: string, patch: Partial<PaymentDraft>) {
    setPayIdemKey(generateIdempotencyKey());
    setPaymentDrafts((prev) => prev.map((p) => (p.id === id ? { ...p, ...patch } : p)));
  }

  function fillRemainingInDraftLine(id: string) {
    setPayIdemKey(generateIdempotencyKey());
    setPaymentDrafts((prev) => {
      const others = prev
        .filter((p) => p.id !== id)
        .reduce((acc, p) => acc + (parseToCents(p.amountInput) ?? 0), 0);
      const rest = Math.max(0, balanceDueCents - others);
      return prev.map((p) =>
        p.id === id ? { ...p, amountInput: centsToApiDecimal(rest) } : p,
      );
    });
  }

  async function handleConfirmPayment() {
    if (!selectedTab || submittingPayment) return;
    setFeedbackMsg(null);

    for (const p of computedPayments) {
      if (p.amountCents <= 0) {
        setFeedbackMsg({
          type: "err",
          text: "Informe um valor de principal maior que R$ 0,00 em cada parcela de pagamento.",
        });
        return;
      }
      if (p.method === "cash" && p.tenderedCents !== null && p.tenderedCents < p.amountCents) {
        setFeedbackMsg({
          type: "err",
          text: "O valor entregue em dinheiro não pode ser menor que a parcela em dinheiro.",
        });
        return;
      }
    }

    if (paymentPrincipalSumCents > balanceDueCents) {
      setFeedbackMsg({
        type: "err",
        text: `O principal informado (${formatBRL(paymentPrincipalSumCents)}) excede o saldo pendente da comanda (${formatBRL(balanceDueCents)}). Para troco em dinheiro, informe o valor entregue no campo "Valor entregue".`,
      });
      return;
    }

    setSubmittingPayment(true);
    try {
      const payload = {
        idempotency_key: payIdemKey,
        payments: computedPayments.map((p) => ({
          method: p.method,
          amount: centsToApiDecimal(p.amountCents),
          cash_tendered:
            p.method === "cash" && p.tenderedCents !== null
              ? centsToApiDecimal(p.tenderedCents)
              : undefined,
        })),
        note: paymentNote.trim() || undefined,
      };
      const resp = await apiFetch<{ tab: TabRecord; idempotent_replay: boolean }>(
        `/tabs/${selectedTab.id}/payments`,
        {
          method: "POST",
          body: JSON.stringify(payload),
        },
      );
      const newBalCents = parseToCents(resp.tab.balance_due) ?? 0;
      setPaymentDrafts([
        {
          id: "tpay-1",
          method: "pix",
          amountInput: newBalCents > 0 ? centsToApiDecimal(newBalCents) : "",
          cashTenderedInput: "",
        },
      ]);
      setPaymentNote("");
      setPayIdemKey(generateIdempotencyKey());
      await onDataChanged();
      setFeedbackMsg({
        type: "ok",
        text:
          newBalCents === 0
            ? `Pagamento registrado! Saldo da Comanda #${resp.tab.number} quitado (R$ 0,00). Você já pode encerrar a comanda.`
            : `Pagamento parcial registrado! Saldo pendente restante da Comanda #${resp.tab.number}: ${formatBRL(resp.tab.balance_due)}.`,
      });
    } catch (err) {
      setFeedbackMsg({
        type: "err",
        text: err instanceof ApiError ? err.message : "Erro ao registrar pagamento na comanda.",
      });
    } finally {
      setSubmittingPayment(false);
    }
  }

  async function handleCloseTab(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedTab || submittingClose) return;
    setSubmittingClose(true);
    setFeedbackMsg(null);
    try {
      const resp = await apiFetch<{ tab: TabRecord }>(`/tabs/${selectedTab.id}/close`, {
        method: "POST",
        body: JSON.stringify({ notes: closeNotes.trim() || undefined }),
      });
      setCloseModalOpen(false);
      setCloseNotes("");
      await onDataChanged();
      setFeedbackMsg({
        type: "ok",
        text: `Comanda #${resp.tab.number} encerrada com sucesso! O número ${resp.tab.number} já pode ser reutilizado para uma nova comanda.`,
      });
    } catch (err) {
      setFeedbackMsg({
        type: "err",
        text: err instanceof ApiError ? err.message : "Não foi possível encerrar a comanda.",
      });
      setCloseModalOpen(false);
    } finally {
      setSubmittingClose(false);
    }
  }

  async function handleVoidLaunch(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedTab || !voidLaunchTarget || submittingVoid) return;
    setVoidError(null);
    if (voidReason.trim().length < 3) {
      setVoidError("Informe o motivo obrigatório do cancelamento (mínimo 3 caracteres).");
      return;
    }
    setSubmittingVoid(true);
    try {
      await apiFetch(`/tabs/${selectedTab.id}/items/${voidLaunchTarget.id}/void`, {
        method: "POST",
        body: JSON.stringify({
          reason: voidReason.trim(),
          customer_refund_completed: voidRefundCompleted,
          customer_refund_method: voidRefundCompleted ? voidRefundMethod : undefined,
          customer_refund_notes:
            voidRefundCompleted && voidRefundNotes.trim() ? voidRefundNotes.trim() : undefined,
        }),
      });
      setVoidLaunchTarget(null);
      setVoidReason("");
      setVoidRefundCompleted(false);
      setVoidRefundNotes("");
      await onDataChanged();
      setFeedbackMsg({
        type: "ok",
        text: `Lançamento #${voidLaunchTarget.id} cancelado com sucesso.`,
      });
    } catch (err) {
      setVoidError(err instanceof ApiError ? err.message : "Erro ao cancelar lançamento.");
    } finally {
      setSubmittingVoid(false);
    }
  }

  async function handleConfirmLaunchRefund(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedTab || !refundLaunchTarget || submittingRefund) return;
    setRefundError(null);
    setSubmittingRefund(true);
    try {
      await apiFetch(`/tabs/${selectedTab.id}/items/${refundLaunchTarget.id}/customer-refund`, {
        method: "POST",
        body: JSON.stringify({
          method: refundMethod,
          notes: refundNotes.trim() || undefined,
        }),
      });
      setRefundLaunchTarget(null);
      setRefundNotes("");
      await onDataChanged();
      setFeedbackMsg({
        type: "ok",
        text: "Devolução financeira ao cliente registrada com sucesso.",
      });
    } catch (err) {
      setRefundError(err instanceof ApiError ? err.message : "Erro ao confirmar devolução.");
    } finally {
      setSubmittingRefund(false);
    }
  }

  return (
    <div className="pos-layout">
      {/* ==================== COLUNA ESQUERDA: ABERTURA E LISTA DE COMANDAS ==================== */}
      <section className="card" aria-label="Listagem e abertura de comandas">
        <h2 style={{ margin: "0 0 0.75rem" }}>Abrir nova comanda</h2>
        {openTabError && (
          <div className="alert alert-error" role="alert">
            {openTabError}
          </div>
        )}
        <form
          onSubmit={handleOpenTab}
          style={{
            display: "grid",
            gridTemplateColumns: "0.7fr 1.3fr auto",
            gap: "0.5rem",
            alignItems: "end",
            marginBottom: "1.25rem",
          }}
        >
          <div>
            <label htmlFor="new-tab-number" className="muted" style={{ fontSize: "0.8rem", display: "block" }}>
              Nº Comanda *
            </label>
            <input
              id="new-tab-number"
              type="number"
              min={1}
              max={999999}
              required
              className="input"
              style={{ width: "100%" }}
              placeholder="Ex.: 12"
              value={newTabNumber}
              onChange={(e) => setNewTabNumber(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="new-tab-customer" className="muted" style={{ fontSize: "0.8rem", display: "block" }}>
              Cliente (opcional)
            </label>
            <input
              id="new-tab-customer"
              type="text"
              className="input"
              style={{ width: "100%" }}
              placeholder="Nome ou referência"
              value={newTabCustomer}
              onChange={(e) => setNewTabCustomer(e.target.value)}
            />
          </div>
          <button type="submit" className="btn" disabled={openingTab}>
            {openingTab ? "Abrindo…" : "+ Abrir Comanda"}
          </button>
        </form>

        <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "1rem 0" }} />

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "0.5rem", flexWrap: "wrap", marginBottom: "0.75rem" }}>
          <h3 style={{ margin: 0, fontSize: "1.05rem" }}>Comandas</h3>
          <div style={{ display: "flex", gap: "0.35rem" }}>
            <button
              type="button"
              className={`btn btn-sm ${statusFilter === "open" ? "" : "btn-secondary"}`}
              onClick={() => setStatusFilter("open")}
            >
              Abertas ({openTabsCount})
            </button>
            <button
              type="button"
              className={`btn btn-sm ${statusFilter === "closed" ? "" : "btn-secondary"}`}
              onClick={() => setStatusFilter("closed")}
            >
              Encerradas ({closedTabsCount})
            </button>
            <button
              type="button"
              className={`btn btn-sm ${statusFilter === "all" ? "" : "btn-secondary"}`}
              onClick={() => setStatusFilter("all")}
            >
              Todas ({tabs.length})
            </button>
          </div>
        </div>

        <input
          type="search"
          className="input"
          style={{ width: "100%", marginBottom: "0.85rem" }}
          placeholder="Buscar comanda por número (ex.: 12) ou nome do cliente…"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          aria-label="Buscar comanda por número ou cliente"
        />

        {filteredTabs.length === 0 ? (
          <p className="muted" style={{ margin: "0.75rem 0" }}>
            Nenhuma comanda encontrada para este filtro.
          </p>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.55rem", maxHeight: 540, overflowY: "auto" }}>
            {filteredTabs.map((t) => {
              const isSelected = selectedTab?.id === t.id;
              const balCents = parseToCents(t.balance_due) ?? 0;
              return (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => selectTab(t)}
                  style={{
                    textAlign: "left",
                    padding: "0.8rem 0.9rem",
                    borderRadius: 12,
                    border: isSelected ? "2px solid var(--primary)" : "1px solid var(--border)",
                    background: isSelected ? "#f5f0ff" : "#fff",
                    cursor: "pointer",
                    font: "inherit",
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "0.5rem" }}>
                    <div>
                      <strong style={{ fontSize: "1.08rem", color: "var(--primary-dark)" }}>
                        Comanda #{t.number}
                      </strong>
                      {t.customer_name && (
                        <span style={{ marginLeft: "0.5rem", fontWeight: 600 }}>
                          — {t.customer_name}
                        </span>
                      )}
                      <span className="muted" style={{ marginLeft: "0.5rem", fontSize: "0.76rem" }}>
                        (ID #{t.id})
                      </span>
                    </div>
                    <span className={`badge ${t.status === "open" ? "badge-warning" : "badge-success"}`}>
                      {t.status === "open" ? "Aberta" : "Encerrada"}
                    </span>
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", marginTop: "0.4rem", fontSize: "0.86rem", flexWrap: "wrap", gap: "0.5rem" }}>
                    <span className="muted">
                      Consumo: <strong>{formatBRL(t.subtotal_consumed)}</strong> · Pago:{" "}
                      <strong>{formatBRL(t.principal_paid)}</strong>
                    </span>
                    <span
                      style={{
                        fontWeight: 800,
                        color: balCents > 0 ? "var(--danger)" : "var(--success)",
                      }}
                    >
                      {balCents > 0 ? `Pendente: ${formatBRL(t.balance_due)}` : "Saldo: R$ 0,00"}
                    </span>
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </section>

      {/* ==================== COLUNA DIREITA: DETALHE E OPERAÇÕES DA COMANDA ==================== */}
      <section className="card" aria-label="Detalhes e operações da comanda selecionada">
        {!selectedTab ? (
          <p className="muted">
            Selecione uma comanda ao lado ou abra uma nova comanda para lançar produtos e registrar pagamentos.
          </p>
        ) : (
          <>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "0.75rem", flexWrap: "wrap" }}>
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", flexWrap: "wrap" }}>
                  <h2 style={{ margin: 0 }}>Comanda #{selectedTab.number}</h2>
                  <span className={`badge ${selectedTab.status === "open" ? "badge-warning" : "badge-success"}`}>
                    {selectedTab.status === "open" ? "Aberta" : "Encerrada (Somente leitura)"}
                  </span>
                  <span className="muted" style={{ fontSize: "0.8rem" }}>
                    ID interno #{selectedTab.id}
                  </span>
                </div>
                <div className="muted" style={{ fontSize: "0.86rem", marginTop: "0.2rem" }}>
                  {selectedTab.customer_name ? `Cliente: ${selectedTab.customer_name} · ` : ""}
                  Aberta em {new Date(selectedTab.opened_at).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })}
                  {selectedTab.closed_at &&
                    ` · Encerrada em ${new Date(selectedTab.closed_at).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })}`}
                </div>
              </div>

              {selectedTab.status === "open" && (
                <button
                  type="button"
                  className="btn btn-success"
                  disabled={!selectedTab.can_close}
                  title={
                    selectedTab.can_close
                      ? "Encerrar comanda com saldo zero"
                      : "Quite o saldo pendente para encerrar a comanda"
                  }
                  onClick={() => {
                    setCloseNotes("");
                    setCloseModalOpen(true);
                  }}
                >
                  Encerrar Comanda #{selectedTab.number}
                </button>
              )}
            </div>

            {/* Painel de destaque de Saldo Pendente e Resumo da Comanda */}
            <div className="totals-box" style={{ marginTop: "0.9rem" }} aria-live="polite">
              <div className="totals-row">
                <span>Consumo ativo acumulado:</span>
                <strong>{formatBRL(selectedTab.subtotal_consumed)}</strong>
              </div>
              <div className="totals-row">
                <span>Principal já quitado:</span>
                <strong style={{ color: "var(--success)" }}>{formatBRL(selectedTab.principal_paid)}</strong>
              </div>
              {selectedTab.card_fee_paid !== "0.00" && (
                <div className="totals-row">
                  <span>Taxas de cartão repassadas:</span>
                  <strong>{formatBRL(selectedTab.card_fee_paid)}</strong>
                </div>
              )}
              <div className="totals-row grand">
                <span>Saldo pendente da comanda:</span>
                <span
                  id="tab-balance-due"
                  style={{
                    color: balanceDueCents > 0 ? "var(--danger)" : "var(--success)",
                  }}
                >
                  {formatBRL(selectedTab.balance_due)}
                </span>
              </div>
            </div>

            {selectedTab.pending_refund_total !== "0.00" && (
              <div className="alert alert-warning" role="alert">
                <strong>Atenção:</strong> Há uma devolução pendente ao cliente de{" "}
                <strong>{formatBRL(selectedTab.pending_refund_total)}</strong> referente a consumo cancelado.
                Confirme a devolução no histórico abaixo para concluir a operação.
              </div>
            )}

            {feedbackMsg && (
              <div
                className={`alert ${feedbackMsg.type === "ok" ? "alert-success" : "alert-error"}`}
                role="status"
              >
                {feedbackMsg.text}
              </div>
            )}

            {/* Operações exclusivas para comandas ABERTAS */}
            {selectedTab.status === "open" && (
              <div style={{ marginTop: "1rem" }}>
                <div style={{ display: "flex", gap: "0.5rem", marginBottom: "0.9rem" }}>
                  <button
                    type="button"
                    className={`btn ${actionMode === "consume" ? "" : "btn-secondary"}`}
                    style={{ flex: 1 }}
                    onClick={() => setActionMode("consume")}
                  >
                    + Lançar Consumo (Produtos / Avulso)
                  </button>
                  <button
                    type="button"
                    className={`btn ${actionMode === "pay" ? "" : "btn-secondary"}`}
                    style={{ flex: 1 }}
                    onClick={() => {
                      setActionMode("pay");
                      if (
                        paymentDrafts.length === 1 &&
                        !paymentDrafts[0].amountInput &&
                        balanceDueCents > 0
                      ) {
                        setPaymentDrafts([
                          {
                            ...paymentDrafts[0],
                            amountInput: centsToApiDecimal(balanceDueCents),
                          },
                        ]);
                      }
                    }}
                  >
                    $ Registrar Pagamento ({formatBRL(selectedTab.balance_due)})
                  </button>
                </div>

                {/* SUB-PAINEL 1: ADICIONAR CONSUMO */}
                {actionMode === "consume" && (
                  <div style={{ padding: "0.9rem", border: "1px solid var(--border)", borderRadius: 12, background: "#fcfaff" }}>
                    <h3 style={{ fontSize: "1rem", margin: "0 0 0.6rem" }}>
                      Novo lançamento de consumo na Comanda #{selectedTab.number}
                    </h3>

                    <div style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: "0.5rem", marginBottom: "0.65rem" }}>
                      <input
                        type="search"
                        className="input"
                        placeholder="Buscar produto…"
                        value={prodSearch}
                        onChange={(e) => setProdSearch(e.target.value)}
                      />
                      <select
                        className="select"
                        value={prodCategory}
                        onChange={(e) => setProdCategory(e.target.value)}
                      >
                        <option value="all">Todas categorias</option>
                        {categories.map((c) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </select>
                    </div>

                    <div className="product-grid" style={{ maxHeight: 230, overflowY: "auto", marginBottom: "0.85rem" }}>
                      {filteredProducts.map((p) => (
                        <button
                          key={p.id}
                          type="button"
                          className="product-btn"
                          style={{ minHeight: 74, padding: "0.65rem" }}
                          onClick={() => addProductToLaunch(p)}
                        >
                          <span className="product-btn-cat">{p.category}</span>
                          <span className="product-btn-name" style={{ fontSize: "0.9rem" }}>{p.name}</span>
                          <span className="product-btn-price" style={{ fontSize: "0.95rem" }}>{formatBRL(p.price)}</span>
                        </button>
                      ))}
                    </div>

                    <form
                      onSubmit={addCustomItemToLaunch}
                      style={{ display: "grid", gridTemplateColumns: "1.5fr 1fr 0.6fr auto", gap: "0.45rem", alignItems: "end", marginBottom: "0.85rem" }}
                    >
                      <div>
                        <label className="muted" style={{ fontSize: "0.76rem", display: "block" }}>Item avulso</label>
                        <input
                          type="text"
                          className="input"
                          style={{ width: "100%" }}
                          placeholder="Descrição avulsa"
                          value={customDesc}
                          onChange={(e) => setCustomDesc(e.target.value)}
                        />
                      </div>
                      <div>
                        <label className="muted" style={{ fontSize: "0.76rem", display: "block" }}>Valor (R$)</label>
                        <input
                          type="text"
                          inputMode="decimal"
                          className="input"
                          style={{ width: "100%" }}
                          placeholder="0,00"
                          value={customAmount}
                          onChange={(e) => setCustomAmount(e.target.value)}
                        />
                      </div>
                      <div>
                        <label className="muted" style={{ fontSize: "0.76rem", display: "block" }}>Qtd.</label>
                        <input
                          type="number"
                          min={1}
                          max={1000}
                          className="input"
                          style={{ width: "100%" }}
                          value={customQty}
                          onChange={(e) => setCustomQty(e.target.value)}
                        />
                      </div>
                      <button type="submit" className="btn btn-secondary btn-sm" style={{ minHeight: 42 }}>
                        + Avulso
                      </button>
                    </form>

                    {launchCart.length > 0 && (
                      <>
                        <div className="cart-list">
                          {launchCart.map((it) => (
                            <div key={it.key} className="cart-item" style={{ background: "#fff" }}>
                              <div>
                                <strong>{it.description}</strong>{" "}
                                {it.kind === "custom" && <span className="badge">Avulso</span>}
                                <div className="muted" style={{ fontSize: "0.8rem" }}>
                                  {formatBRL(it.unitPriceCents)} × {it.quantity} ={" "}
                                  <strong>{formatBRL(it.unitPriceCents * it.quantity)}</strong>
                                </div>
                              </div>
                              <div className="qty-controls">
                                <button
                                  type="button"
                                  className="qty-btn"
                                  onClick={() => updateLaunchQty(it.key, -1)}
                                >
                                  −
                                </button>
                                <span style={{ minWidth: 22, textAlign: "center", fontWeight: 700 }}>
                                  {it.quantity}
                                </span>
                                <button
                                  type="button"
                                  className="qty-btn"
                                  onClick={() => updateLaunchQty(it.key, 1)}
                                >
                                  +
                                </button>
                              </div>
                            </div>
                          ))}
                        </div>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: "0.65rem" }}>
                          <div>
                            Subtotal deste lançamento:{" "}
                            <strong style={{ fontSize: "1.1rem", color: "var(--primary-dark)" }}>
                              {formatBRL(launchSubtotalCents)}
                            </strong>
                          </div>
                          <button
                            type="button"
                            className="btn"
                            disabled={submittingLaunch}
                            onClick={handleConfirmLaunch}
                          >
                            {submittingLaunch
                              ? "Registrando…"
                              : `Confirmar Lançamento (${formatBRL(launchSubtotalCents)})`}
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                )}

                {/* SUB-PAINEL 2: REGISTRAR PAGAMENTO PARCIAL OU INTEGRAL */}
                {actionMode === "pay" && (
                  <div style={{ padding: "0.9rem", border: "1px solid var(--border)", borderRadius: 12, background: "#fcfaff" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.65rem" }}>
                      <h3 style={{ fontSize: "1rem", margin: 0 }}>
                        Registrar recebimento — Comanda #{selectedTab.number}
                      </h3>
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        onClick={addPaymentDraftLine}
                        disabled={balanceDueCents <= 0}
                      >
                        + Dividir pagamento
                      </button>
                    </div>

                    {balanceDueCents <= 0 ? (
                      <p className="muted" style={{ margin: 0 }}>
                        Esta comanda já está com saldo R$ 0,00. Você pode encerrá-la no botão acima ou lançar novos consumos.
                      </p>
                    ) : (
                      <>
                        {computedPayments.map((p, idx) => (
                          <div key={p.id} className="payment-row">
                            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr auto", gap: "0.5rem", alignItems: "end" }}>
                              <div>
                                <label className="muted" style={{ fontSize: "0.78rem", display: "block" }}>
                                  Método #{idx + 1}
                                </label>
                                <select
                                  className="select"
                                  style={{ width: "100%" }}
                                  value={p.method}
                                  onChange={(e) =>
                                    updatePaymentDraftLine(p.id, {
                                      method: e.target.value as PaymentMethod,
                                      cashTenderedInput: "",
                                    })
                                  }
                                >
                                  <option value="pix">Pix</option>
                                  <option value="card">Cartão</option>
                                  <option value="cash">Dinheiro</option>
                                </select>
                              </div>
                              <div>
                                <label className="muted" style={{ fontSize: "0.78rem", display: "block" }}>
                                  Principal a abater da comanda (R$)
                                </label>
                                <input
                                  type="text"
                                  inputMode="decimal"
                                  className="input"
                                  style={{ width: "100%" }}
                                  placeholder="0,00"
                                  value={p.amountInput}
                                  onChange={(e) =>
                                    updatePaymentDraftLine(p.id, { amountInput: e.target.value })
                                  }
                                />
                              </div>
                              <div style={{ display: "flex", gap: "0.35rem" }}>
                                <button
                                  type="button"
                                  className="btn btn-secondary btn-sm"
                                  onClick={() => fillRemainingInDraftLine(p.id)}
                                >
                                  Saldo total
                                </button>
                                {computedPayments.length > 1 && (
                                  <button
                                    type="button"
                                    className="btn btn-secondary btn-sm"
                                    style={{ color: "var(--danger)" }}
                                    onClick={() => removePaymentDraftLine(p.id)}
                                  >
                                    ✕
                                  </button>
                                )}
                              </div>
                            </div>

                            {p.method === "card" && p.amountCents > 0 && (
                              <div className="muted" style={{ fontSize: "0.83rem", marginTop: "0.45rem" }}>
                                Principal abatido: <strong>{formatBRL(p.amountCents)}</strong> · Taxa cartão (
                                {settings.card_fee_percent.replace(".", ",")}%):{" "}
                                <strong>{formatBRL(p.feeCents)}</strong> · Cobrar na maquininha:{" "}
                                <strong style={{ color: "var(--primary-dark)" }}>{formatBRL(p.grossCents)}</strong>
                              </div>
                            )}

                            {p.method === "cash" && (
                              <div style={{ marginTop: "0.55rem", display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem", alignItems: "center" }}>
                                <div>
                                  <label className="muted" style={{ fontSize: "0.78rem", display: "block" }}>
                                    Valor entregue em dinheiro (R$)
                                  </label>
                                  <input
                                    type="text"
                                    inputMode="decimal"
                                    className="input"
                                    style={{ width: "100%" }}
                                    placeholder={centsToApiDecimal(p.amountCents).replace(".", ",")}
                                    value={p.cashTenderedInput}
                                    onChange={(e) =>
                                      updatePaymentDraftLine(p.id, { cashTenderedInput: e.target.value })
                                    }
                                  />
                                </div>
                                <div style={{ fontSize: "0.9rem" }}>
                                  {p.changeCents !== null && p.changeCents >= 0 ? (
                                    <div>
                                      Troco:{" "}
                                      <strong style={{ color: "var(--success)" }}>
                                        {formatBRL(p.changeCents)}
                                      </strong>
                                    </div>
                                  ) : (
                                    <div style={{ color: "var(--danger)", fontWeight: 600 }}>
                                      Valor entregue insuficiente
                                    </div>
                                  )}
                                </div>
                              </div>
                            )}
                          </div>
                        ))}

                        <div className="totals-box" style={{ background: "#fff" }}>
                          <div className="totals-row">
                            <span>Principal que será abatido agora:</span>
                            <strong>{formatBRL(paymentPrincipalSumCents)}</strong>
                          </div>
                          {paymentFeeSumCents > 0 && (
                            <div className="totals-row">
                              <span>Taxa de cartão sobre a parcela cartão:</span>
                              <strong>{formatBRL(paymentFeeSumCents)}</strong>
                            </div>
                          )}
                          <div className="totals-row">
                            <span>Total a cobrar do cliente agora:</span>
                            <strong style={{ color: "var(--primary-dark)", fontSize: "1.08rem" }}>
                              {formatBRL(paymentGrossSumCents)}
                            </strong>
                          </div>
                          <div className="totals-row">
                            <span>Saldo pendente após este pagamento:</span>
                            <strong
                              style={{
                                color:
                                  remainingBalanceAfterPayCents < 0
                                    ? "var(--danger)"
                                    : remainingBalanceAfterPayCents === 0
                                      ? "var(--success)"
                                      : "var(--warning)",
                              }}
                            >
                              {remainingBalanceAfterPayCents >= 0
                                ? formatBRL(remainingBalanceAfterPayCents)
                                : `Excede em ${formatBRL(-remainingBalanceAfterPayCents)}`}
                            </strong>
                          </div>
                        </div>

                        {paymentChangeSumCents > 0 && (
                          <div className="change-banner">
                            <span>Troco a devolver ao cliente:</span>
                            <span>{formatBRL(paymentChangeSumCents)}</span>
                          </div>
                        )}

                        <button
                          type="button"
                          className="btn btn-success"
                          style={{ width: "100%" }}
                          disabled={submittingPayment || paymentPrincipalSumCents <= 0}
                          onClick={handleConfirmPayment}
                        >
                          {submittingPayment
                            ? "Registrando pagamento…"
                            : `Confirmar Recebimento (${formatBRL(paymentGrossSumCents)})`}
                        </button>
                      </>
                    )}
                  </div>
                )}
              </div>
            )}

            {/* HISTÓRICO DE LANÇAMENTOS E PAGAMENTOS DA COMANDA */}
            <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "1.25rem 0 1rem" }} />
            <h3 style={{ fontSize: "1rem", marginBottom: "0.5rem" }}>
              Histórico de lançamentos de consumo ({selectedTab.launches.length})
            </h3>
            {selectedTab.launches.length === 0 ? (
              <p className="muted" style={{ fontSize: "0.9rem" }}>
                Nenhum lançamento de consumo registrado nesta comanda.
              </p>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", marginBottom: "1rem" }}>
                {selectedTab.launches.map((l, idx) => (
                  <div
                    key={l.id}
                    style={{
                      padding: "0.65rem 0.8rem",
                      border: "1px solid var(--border)",
                      borderRadius: 10,
                      background: l.status === "voided" ? "#fff5f5" : "#fff",
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "0.5rem" }}>
                      <div>
                        <strong>Lançamento #{idx + 1}</strong>{" "}
                        <span className="muted" style={{ fontSize: "0.8rem" }}>
                          (Reg. #{l.id} às{" "}
                          {new Date(l.created_at).toLocaleTimeString("pt-BR", {
                            hour: "2-digit",
                            minute: "2-digit",
                          })}
                          )
                        </span>
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                        <strong>{formatBRL(l.subtotal)}</strong>
                        {l.status === "active" ? (
                          <span className="badge badge-success">Ativo</span>
                        ) : (
                          <span className="badge badge-danger">Cancelado</span>
                        )}
                        {isAdmin && selectedTab.status === "open" && l.status === "active" && (
                          <button
                            type="button"
                            className="btn btn-danger btn-sm"
                            onClick={() => {
                              setVoidLaunchTarget(l);
                              setVoidReason("");
                              setVoidRefundCompleted(false);
                              setVoidRefundNotes("");
                              setVoidError(null);
                            }}
                          >
                            Cancelar
                          </button>
                        )}
                        {isAdmin &&
                          selectedTab.status === "open" &&
                          l.status === "voided" &&
                          l.customer_refund_status === "pending" && (
                            <button
                              type="button"
                              className="btn btn-secondary btn-sm"
                              onClick={() => {
                                setRefundLaunchTarget(l);
                                setRefundMethod("pix");
                                setRefundNotes("");
                                setRefundError(null);
                              }}
                            >
                              Confirmar devolução ({formatBRL(l.customer_refund_amount ?? "0.00")})
                            </button>
                          )}
                      </div>
                    </div>
                    <div style={{ marginTop: "0.35rem", fontSize: "0.86rem" }}>
                      {l.items.map((it) => (
                        <div key={it.id}>
                          {it.quantity}× {it.description} ({formatBRL(it.unit_price)} un.) ={" "}
                          <strong>{formatBRL(it.line_total)}</strong>
                        </div>
                      ))}
                    </div>
                    {l.status === "voided" && (
                      <div className="muted" style={{ fontSize: "0.8rem", marginTop: "0.3rem" }}>
                        Motivo do cancelamento: {l.void_reason}
                        {l.customer_refund_status === "pending" &&
                          ` · Devolução pendente ao cliente: ${formatBRL(l.customer_refund_amount ?? "0.00")}`}
                        {l.customer_refund_status === "completed" &&
                          ` · Devolvido ao cliente: ${formatBRL(l.customer_refund_amount ?? "0.00")}`}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}

            <h3 style={{ fontSize: "1rem", marginBottom: "0.5rem" }}>
              Histórico de pagamentos e movimentos ({selectedTab.payments.length})
            </h3>
            {selectedTab.payments.length === 0 ? (
              <p className="muted" style={{ fontSize: "0.9rem" }}>
                Nenhum pagamento registrado nesta comanda até o momento.
              </p>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: "0.45rem" }}>
                {selectedTab.payments.map((m) => (
                  <div
                    key={m.id}
                    style={{
                      padding: "0.6rem 0.8rem",
                      border: "1px solid var(--border)",
                      borderRadius: 10,
                      background: m.direction === "out" ? "#fff5f5" : "#fff",
                      fontSize: "0.88rem",
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      flexWrap: "wrap",
                      gap: "0.5rem",
                    }}
                  >
                    <div>
                      <span className={`badge ${m.direction === "in" ? "badge-success" : "badge-danger"}`}>
                        {m.direction === "in" ? "Recebimento" : "Devolução / Saída"}
                      </span>{" "}
                      <strong style={{ marginLeft: "0.35rem" }}>{methodLabel(m.method)}</strong>
                      <span className="muted" style={{ marginLeft: "0.4rem", fontSize: "0.8rem" }}>
                        às{" "}
                        {new Date(m.created_at).toLocaleTimeString("pt-BR", {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </span>
                      {m.cash_tendered && (
                        <div className="muted" style={{ fontSize: "0.8rem", marginTop: "0.2rem" }}>
                          Entregue: {formatBRL(m.cash_tendered)} · Troco:{" "}
                          <strong>{formatBRL(m.change_given ?? "0.00")}</strong>
                        </div>
                      )}
                    </div>
                    <div style={{ textAlign: "right" }}>
                      <div>
                        Principal: <strong>{formatBRL(m.amount)}</strong>
                        {m.fee_amount !== "0.00" && ` + Taxa: ${formatBRL(m.fee_amount)}`}
                      </div>
                      <div style={{ fontWeight: 700, color: "var(--primary-dark)" }}>
                        Total bruto: {formatBRL(m.gross_amount)}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </section>

      {/* ==================== MODAL: CONFIRMAR ENCERRAMENTO DA COMANDA ==================== */}
      {closeModalOpen && selectedTab && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="close-tab-title">
          <div className="modal-card">
            <h2 id="close-tab-title">Encerrar Comanda #{selectedTab.number}</h2>
            <p className="muted">
              Após o encerramento, esta comanda ficará disponível apenas para consulta (somente leitura) e o número{" "}
              <strong>#{selectedTab.number}</strong> poderá ser reutilizado para um novo atendimento.
            </p>

            <div className="totals-box">
              <div className="totals-row">
                <span>Consumo total ativo:</span>
                <strong>{formatBRL(selectedTab.subtotal_consumed)}</strong>
              </div>
              <div className="totals-row">
                <span>Principal quitado:</span>
                <strong>{formatBRL(selectedTab.principal_paid)}</strong>
              </div>
              <div className="totals-row">
                <span>Taxas de cartão recebidas:</span>
                <strong>{formatBRL(selectedTab.card_fee_paid)}</strong>
              </div>
              <div className="totals-row grand">
                <span>Saldo pendente:</span>
                <span style={{ color: "var(--success)" }}>{formatBRL(selectedTab.balance_due)}</span>
              </div>
            </div>

            <form onSubmit={handleCloseTab}>
              <div className="field">
                <label htmlFor="close-tab-notes">Observação de encerramento (opcional)</label>
                <input
                  id="close-tab-notes"
                  type="text"
                  className="input"
                  placeholder="Ex.: Ficha física recolhida no caixa"
                  value={closeNotes}
                  onChange={(e) => setCloseNotes(e.target.value)}
                />
              </div>
              <div style={{ display: "flex", gap: "0.6rem", justifyContent: "flex-end", marginTop: "1rem" }}>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={submittingClose}
                  onClick={() => setCloseModalOpen(false)}
                >
                  Voltar
                </button>
                <button type="submit" className="btn btn-success" disabled={submittingClose}>
                  {submittingClose ? "Encerrando…" : "Confirmar Encerramento"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* ==================== MODAL: CANCELAR LANÇAMENTO DE CONSUMO (ADMIN) ==================== */}
      {voidLaunchTarget && selectedTab && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="void-launch-title">
          <div className="modal-card">
            <h2 id="void-launch-title">
              Cancelar lançamento #{voidLaunchTarget.id} — Comanda #{selectedTab.number}
            </h2>
            <p className="muted">
              Valor do lançamento: <strong>{formatBRL(voidLaunchTarget.subtotal)}</strong>. O registro permanecerá no histórico para auditoria.
            </p>
            {voidError && <div className="alert alert-error">{voidError}</div>}

            <form onSubmit={handleVoidLaunch}>
              <div className="field">
                <label htmlFor="void-launch-reason">Motivo obrigatório do cancelamento</label>
                <input
                  id="void-launch-reason"
                  type="text"
                  className="input"
                  required
                  placeholder="Ex.: Produto lançado na comanda errada"
                  value={voidReason}
                  onChange={(e) => setVoidReason(e.target.value)}
                />
              </div>

              <div className="field" style={{ flexDirection: "row", alignItems: "center", gap: "0.5rem" }}>
                <input
                  id="void-launch-refund-done"
                  type="checkbox"
                  checked={voidRefundCompleted}
                  onChange={(e) => setVoidRefundCompleted(e.target.checked)}
                />
                <label htmlFor="void-launch-refund-done" style={{ margin: 0 }}>
                  Caso este cancelamento gere excesso de valor já pago pelo cliente, a devolução financeira já foi realizada agora
                </label>
              </div>

              {voidRefundCompleted && (
                <>
                  <div className="field">
                    <label htmlFor="void-launch-refund-method">Meio utilizado na devolução</label>
                    <select
                      id="void-launch-refund-method"
                      className="select"
                      value={voidRefundMethod}
                      onChange={(e) => setVoidRefundMethod(e.target.value as PaymentMethod)}
                    >
                      <option value="pix">Pix</option>
                      <option value="card">Estorno na maquininha (Cartão)</option>
                      <option value="cash">Dinheiro</option>
                    </select>
                  </div>
                  <div className="field">
                    <label htmlFor="void-launch-refund-notes">Observação (opcional)</label>
                    <input
                      id="void-launch-refund-notes"
                      type="text"
                      className="input"
                      value={voidRefundNotes}
                      onChange={(e) => setVoidRefundNotes(e.target.value)}
                    />
                  </div>
                </>
              )}

              <div style={{ display: "flex", gap: "0.6rem", justifyContent: "flex-end", marginTop: "1rem" }}>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={submittingVoid}
                  onClick={() => setVoidLaunchTarget(null)}
                >
                  Voltar
                </button>
                <button type="submit" className="btn btn-danger" disabled={submittingVoid}>
                  {submittingVoid ? "Cancelando…" : "Confirmar Cancelamento"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* ==================== MODAL: CONFIRMAR DEVOLUÇÃO EFETIVA DE LANÇAMENTO ==================== */}
      {refundLaunchTarget && selectedTab && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="refund-launch-title">
          <div className="modal-card">
            <h2 id="refund-launch-title">
              Confirmar devolução ao cliente — Comanda #{selectedTab.number}
            </h2>
            <p className="muted">
              Confirme que o valor excedente de{" "}
              <strong>{formatBRL(refundLaunchTarget.customer_refund_amount ?? "0.00")}</strong> foi efetivamente devolvido ao cliente.
            </p>
            {refundError && <div className="alert alert-error">{refundError}</div>}

            <form onSubmit={handleConfirmLaunchRefund}>
              <div className="field">
                <label htmlFor="refund-launch-method">Meio utilizado na devolução</label>
                <select
                  id="refund-launch-method"
                  className="select"
                  value={refundMethod}
                  onChange={(e) => setRefundMethod(e.target.value as PaymentMethod)}
                >
                  <option value="pix">Pix</option>
                  <option value="card">Estorno na maquininha (Cartão)</option>
                  <option value="cash">Dinheiro</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="refund-launch-notes">Observação (opcional)</label>
                <input
                  id="refund-launch-notes"
                  type="text"
                  className="input"
                  value={refundNotes}
                  onChange={(e) => setRefundNotes(e.target.value)}
                />
              </div>
              <div style={{ display: "flex", gap: "0.6rem", justifyContent: "flex-end", marginTop: "1rem" }}>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={submittingRefund}
                  onClick={() => setRefundLaunchTarget(null)}
                >
                  Cancelar
                </button>
                <button type="submit" className="btn btn-success" disabled={submittingRefund}>
                  {submittingRefund ? "Registrando…" : "Confirmar Devolução"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
