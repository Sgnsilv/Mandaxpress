"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  CardFeeMode,
  FinancialSummary,
  PaymentMethod,
  PaymentSettings,
  Product,
  SaleRecord,
  calculateCardFeeCents,
  centsToApiDecimal,
  formatBRL,
  generateIdempotencyKey,
  methodLabel,
  parseToCents,
} from "@/lib/money";

type ActiveTab = "pos" | "history" | "products" | "settings";

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

export default function HomePage() {
  const { state } = useAuth();
  const [activeTab, setActiveTab] = useState<ActiveTab>("pos");

  const [products, setProducts] = useState<Product[]>([]);
  const [settings, setSettings] = useState<PaymentSettings>({
    card_fee_percent: "0.00",
    card_fee_mode: "simple_addition",
  });
  const [sales, setSales] = useState<SaleRecord[]>([]);
  const [summary, setSummary] = useState<FinancialSummary | null>(null);
  const [loadingData, setLoadingData] = useState(true);
  const [globalError, setGlobalError] = useState<string | null>(null);

  // Busca de produtos no caixa
  const [posSearch, setPosSearch] = useState("");
  const [posCategory, setPosCategory] = useState<string>("all");

  // Carrinho da venda rápida
  const [cart, setCart] = useState<CartItem[]>([]);
  const [customDesc, setCustomDesc] = useState("");
  const [customAmount, setCustomAmount] = useState("");
  const [customQty, setCustomQty] = useState("1");

  // Pagamentos da venda rápida
  const [payments, setPayments] = useState<PaymentDraft[]>([
    { id: "pay-1", method: "pix", amountInput: "", cashTenderedInput: "" },
  ]);

  // Idempotência e confirmação da venda rápida
  const [idempotencyKey, setIdempotencyKey] = useState<string>(() => generateIdempotencyKey());
  const [reviewModalOpen, setReviewModalOpen] = useState(false);
  const [submittingSale, setSubmittingSale] = useState(false);
  const [saleError, setSaleError] = useState<string | null>(null);
  const [confirmedSale, setConfirmedSale] = useState<SaleRecord | null>(null);

  // Gerenciamento de produtos (admin)
  const [adminSearch, setAdminSearch] = useState("");
  const [editingProduct, setEditingProduct] = useState<Product | null>(null);
  const [prodName, setProdName] = useState("");
  const [prodCategory, setProdCategory] = useState("Açaí");
  const [prodPrice, setProdPrice] = useState("");
  const [savingProduct, setSavingProduct] = useState(false);
  const [productMsg, setProductMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);

  // Configuração de taxa (admin)
  const [feePercentInput, setFeePercentInput] = useState("0.00");
  const [feeModeInput, setFeeModeInput] = useState<CardFeeMode>("simple_addition");
  const [savingSettings, setSavingSettings] = useState(false);
  const [settingsMsg, setSettingsMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);

  // Estorno e devolução efetiva (admin)
  const [voidTarget, setVoidTarget] = useState<SaleRecord | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [voidRefundCompleted, setVoidRefundCompleted] = useState(false);
  const [voidRefundMethod, setVoidRefundMethod] = useState<PaymentMethod>("pix");
  const [voidRefundNotes, setVoidRefundNotes] = useState("");
  const [submittingVoid, setSubmittingVoid] = useState(false);
  const [voidError, setVoidError] = useState<string | null>(null);

  const [refundTarget, setRefundTarget] = useState<SaleRecord | null>(null);
  const [refundMethod, setRefundMethod] = useState<PaymentMethod>("pix");
  const [refundNotes, setRefundNotes] = useState("");
  const [submittingRefund, setSubmittingRefund] = useState(false);
  const [refundError, setRefundError] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    setGlobalError(null);
    try {
      const [prodResp, setResp, salesResp] = await Promise.all([
        apiFetch<{ products: Product[] }>("/products"),
        apiFetch<{ settings: PaymentSettings }>("/settings/payment"),
        apiFetch<{ sales: SaleRecord[]; summary: FinancialSummary }>("/sales?limit=50"),
      ]);
      setProducts(prodResp.products);
      setSettings(setResp.settings);
      setFeePercentInput(setResp.settings.card_fee_percent);
      setFeeModeInput(setResp.settings.card_fee_mode);
      setSales(salesResp.sales);
      setSummary(salesResp.summary);
    } catch (err) {
      setGlobalError(err instanceof ApiError ? err.message : "Erro ao carregar dados do caixa.");
    } finally {
      setLoadingData(false);
    }
  }, []);

  useEffect(() => {
    if (state.status === "authenticated") {
      loadAll();
    }
  }, [state.status, loadAll]);

  // Reinicia a chave de idempotência quando o conteúdo do carrinho ou pagamentos é alterado pelo operador
  const regenerateIdempotencyKey = useCallback(() => {
    setIdempotencyKey(generateIdempotencyKey());
  }, []);

  const activeProducts = useMemo(() => products.filter((p) => p.is_active), [products]);

  const categories = useMemo(() => {
    const set = new Set<string>();
    for (const p of activeProducts) set.add(p.category);
    return Array.from(set).sort();
  }, [activeProducts]);

  const filteredPosProducts = useMemo(() => {
    const q = posSearch.trim().toLowerCase();
    return activeProducts.filter((p) => {
      if (posCategory !== "all" && p.category !== posCategory) return false;
      if (q && !p.name.toLowerCase().includes(q) && !p.category.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [activeProducts, posSearch, posCategory]);

  const filteredAdminProducts = useMemo(() => {
    const q = adminSearch.trim().toLowerCase();
    if (!q) return products;
    return products.filter(
      (p) => p.name.toLowerCase().includes(q) || p.category.toLowerCase().includes(q),
    );
  }, [products, adminSearch]);

  // Cálculos do carrinho
  const subtotalCents = useMemo(
    () => cart.reduce((acc, item) => acc + item.unitPriceCents * item.quantity, 0),
    [cart],
  );

  // Se houver apenas 1 linha de pagamento e o operador adicionar/remover produtos, podemos facilitar com botão "Quitar restante"
  const computedPayments = useMemo(() => {
    return payments.map((p) => {
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
  }, [payments, settings]);

  const totalPrincipalAllocatedCents = useMemo(
    () => computedPayments.reduce((acc, p) => acc + p.amountCents, 0),
    [computedPayments],
  );

  const totalCardFeeCents = useMemo(
    () => computedPayments.reduce((acc, p) => acc + p.feeCents, 0),
    [computedPayments],
  );

  const totalChargedCents = subtotalCents + totalCardFeeCents;
  const remainingPrincipalCents = subtotalCents - totalPrincipalAllocatedCents;

  const totalChangeCents = useMemo(
    () =>
      computedPayments.reduce(
        (acc, p) => acc + (p.changeCents !== null && p.changeCents > 0 ? p.changeCents : 0),
        0,
      ),
    [computedPayments],
  );

  function addProductToCart(product: Product) {
    const unitPriceCents = parseToCents(product.price) ?? 0;
    setConfirmedSale(null);
    setSaleError(null);
    regenerateIdempotencyKey();

    setCart((prev) => {
      const next = prev.map((it) => ({ ...it }));
      const idx = next.findIndex((it) => it.kind === "product" && it.productId === product.id);
      if (idx >= 0) {
        next[idx].quantity += 1;
      } else {
        next.push({
          key: `prod-${product.id}`,
          kind: "product",
          productId: product.id,
          description: product.name,
          unitPriceCents,
          quantity: 1,
        });
      }
      const newSubtotal = next.reduce((acc, it) => acc + it.unitPriceCents * it.quantity, 0);
      setPayments((currPays) => {
        if (currPays.length === 1) {
          return [{ ...currPays[0], amountInput: centsToApiDecimal(newSubtotal) }];
        }
        return currPays;
      });
      return next;
    });
  }

  function updateCartQuantity(key: string, delta: number) {
    setConfirmedSale(null);
    setSaleError(null);
    regenerateIdempotencyKey();

    setCart((prev) => {
      const next: CartItem[] = [];
      for (const item of prev) {
        if (item.key !== key) {
          next.push(item);
          continue;
        }
        const newQty = item.quantity + delta;
        if (newQty >= 1) {
          next.push({ ...item, quantity: newQty });
        }
      }
      const newSubtotal = next.reduce((acc, it) => acc + it.unitPriceCents * it.quantity, 0);
      setPayments((currPays) => {
        if (currPays.length === 1) {
          return [
            {
              ...currPays[0],
              amountInput: newSubtotal > 0 ? centsToApiDecimal(newSubtotal) : "",
            },
          ];
        }
        return currPays;
      });
      return next;
    });
  }

  function removeCartItem(key: string) {
    updateCartQuantity(key, -999999);
  }

  function addCustomItemToCart(e: React.FormEvent) {
    e.preventDefault();
    setSaleError(null);
    setConfirmedSale(null);
    const desc = customDesc.trim();
    const amountCents = parseToCents(customAmount);
    const qty = Number(customQty);

    if (desc.length < 2) {
      setSaleError("Informe uma descrição válida (mínimo 2 caracteres) para o item avulso.");
      return;
    }
    if (amountCents === null || amountCents <= 0) {
      setSaleError("Informe um valor unitário maior que R$ 0,00 para o item avulso.");
      return;
    }
    if (!Number.isInteger(qty) || qty < 1 || qty > 1000) {
      setSaleError("Informe uma quantidade inteira entre 1 e 1000.");
      return;
    }

    regenerateIdempotencyKey();
    setCart((prev) => {
      const next = [
        ...prev,
        {
          key: `custom-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
          kind: "custom" as const,
          description: desc,
          unitPriceCents: amountCents,
          quantity: qty,
        },
      ];
      const newSubtotal = next.reduce((acc, it) => acc + it.unitPriceCents * it.quantity, 0);
      setPayments((currPays) => {
        if (currPays.length === 1) {
          return [{ ...currPays[0], amountInput: centsToApiDecimal(newSubtotal) }];
        }
        return currPays;
      });
      return next;
    });
    setCustomDesc("");
    setCustomAmount("");
    setCustomQty("1");
  }

  function addPaymentLine() {
    regenerateIdempotencyKey();
    const rest = Math.max(0, subtotalCents - totalPrincipalAllocatedCents);
    setPayments((prev) => [
      ...prev,
      {
        id: `pay-${Date.now()}-${prev.length + 1}`,
        method: "cash",
        amountInput: rest > 0 ? centsToApiDecimal(rest) : "",
        cashTenderedInput: "",
      },
    ]);
  }

  function removePaymentLine(id: string) {
    regenerateIdempotencyKey();
    setPayments((prev) => (prev.length <= 1 ? prev : prev.filter((p) => p.id !== id)));
  }

  function updatePaymentLine(id: string, patch: Partial<PaymentDraft>) {
    regenerateIdempotencyKey();
    setPayments((prev) => prev.map((p) => (p.id === id ? { ...p, ...patch } : p)));
  }

  function fillRemainingInLine(id: string) {
    regenerateIdempotencyKey();
    setPayments((prev) => {
      const othersSum = prev
        .filter((p) => p.id !== id)
        .reduce((acc, p) => acc + (parseToCents(p.amountInput) ?? 0), 0);
      const rest = Math.max(0, subtotalCents - othersSum);
      return prev.map((p) =>
        p.id === id ? { ...p, amountInput: centsToApiDecimal(rest) } : p,
      );
    });
  }

  function openSaleConfirmationModal() {
    setSaleError(null);
    if (cart.length === 0 || subtotalCents <= 0) {
      setSaleError("Adicione pelo menos um item ao carrinho antes de finalizar.");
      return;
    }
    for (const p of computedPayments) {
      if (p.amountCents <= 0) {
        setSaleError("Todas as parcelas de pagamento devem ter valor maior que R$ 0,00.");
        return;
      }
      if (p.method === "cash" && p.tenderedCents !== null && p.tenderedCents < p.amountCents) {
        setSaleError("O valor entregue em dinheiro não pode ser menor que a parcela em dinheiro.");
        return;
      }
    }
    if (remainingPrincipalCents !== 0) {
      if (remainingPrincipalCents > 0) {
        setSaleError(
          `Ainda faltam ${formatBRL(remainingPrincipalCents)} para quitar o subtotal dos produtos.`,
        );
      } else {
        setSaleError(
          `A soma das parcelas dos produtos excede o subtotal em ${formatBRL(-remainingPrincipalCents)}. Para troco em dinheiro, informe o valor entregue no campo "Valor entregue".`,
        );
      }
      return;
    }
    setReviewModalOpen(true);
  }

  async function submitQuickSale() {
    if (submittingSale) return;
    setSubmittingSale(true);
    setSaleError(null);
    try {
      const payload = {
        idempotency_key: idempotencyKey,
        items: cart.map((item) =>
          item.kind === "product"
            ? { product_id: item.productId, quantity: item.quantity }
            : {
                custom_description: item.description,
                custom_amount: centsToApiDecimal(item.unitPriceCents),
                quantity: item.quantity,
              },
        ),
        payments: computedPayments.map((p) => ({
          method: p.method,
          amount: centsToApiDecimal(p.amountCents),
          cash_tendered:
            p.method === "cash" && p.tenderedCents !== null
              ? centsToApiDecimal(p.tenderedCents)
              : undefined,
        })),
      };

      const resp = await apiFetch<{ sale: SaleRecord; idempotent_replay: boolean }>(
        "/sales/quick",
        {
          method: "POST",
          body: JSON.stringify(payload),
        },
      );

      // Limpa o carrinho SOMENTE após confirmação bem-sucedida
      setCart([]);
      setPayments([{ id: "pay-1", method: "pix", amountInput: "", cashTenderedInput: "" }]);
      setIdempotencyKey(generateIdempotencyKey());
      setReviewModalOpen(false);
      setConfirmedSale(resp.sale);
      await loadAll();
    } catch (err) {
      setSaleError(
        err instanceof ApiError ? err.message : "Falha ao registrar a venda. Seus itens foram mantidos no carrinho.",
      );
    } finally {
      setSubmittingSale(false);
    }
  }

  // Funções de gerenciamento de produtos (Admin)
  function startEditProduct(p: Product) {
    setEditingProduct(p);
    setProdName(p.name);
    setProdCategory(p.category);
    setProdPrice(p.price);
    setProductMsg(null);
  }

  function resetProductForm() {
    setEditingProduct(null);
    setProdName("");
    setProdCategory("Açaí");
    setProdPrice("");
  }

  async function handleSaveProduct(e: React.FormEvent) {
    e.preventDefault();
    if (savingProduct) return;
    setProductMsg(null);

    const priceCents = parseToCents(prodPrice);
    if (priceCents === null) {
      setProductMsg({ type: "err", text: "Informe um preço válido (ex.: 18,50)." });
      return;
    }

    setSavingProduct(true);
    try {
      const body = {
        name: prodName.trim(),
        category: prodCategory.trim() || "Geral",
        price: centsToApiDecimal(priceCents),
      };
      if (editingProduct) {
        await apiFetch(`/products/${editingProduct.id}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
        setProductMsg({ type: "ok", text: `Produto "${body.name}" atualizado com sucesso.` });
      } else {
        await apiFetch("/products", {
          method: "POST",
          body: JSON.stringify({ ...body, is_active: true }),
        });
        setProductMsg({ type: "ok", text: `Produto "${body.name}" cadastrado com sucesso.` });
      }
      resetProductForm();
      await loadAll();
    } catch (err) {
      setProductMsg({
        type: "err",
        text: err instanceof ApiError ? err.message : "Erro ao salvar produto.",
      });
    } finally {
      setSavingProduct(false);
    }
  }

  async function toggleProductActive(p: Product) {
    setProductMsg(null);
    try {
      await apiFetch(`/products/${p.id}/status`, {
        method: "PATCH",
        body: JSON.stringify({ is_active: !p.is_active }),
      });
      await loadAll();
    } catch (err) {
      setProductMsg({
        type: "err",
        text: err instanceof ApiError ? err.message : "Não foi possível alterar o status do produto.",
      });
    }
  }

  // Salvar configuração de taxa (Admin)
  async function handleSaveSettings(e: React.FormEvent) {
    e.preventDefault();
    if (savingSettings) return;
    setSettingsMsg(null);
    const pctCents = parseToCents(feePercentInput);
    if (pctCents === null || pctCents >= 10000) {
      setSettingsMsg({ type: "err", text: "Informe um percentual entre 0,00% e 99,99%." });
      return;
    }

    setSavingSettings(true);
    try {
      const resp = await apiFetch<{ settings: PaymentSettings }>("/settings/payment", {
        method: "PUT",
        body: JSON.stringify({
          card_fee_percent: centsToApiDecimal(pctCents),
          card_fee_mode: feeModeInput,
        }),
      });
      setSettings(resp.settings);
      setFeePercentInput(resp.settings.card_fee_percent);
      setFeeModeInput(resp.settings.card_fee_mode);
      setSettingsMsg({ type: "ok", text: "Configuração de taxa do cartão atualizada." });
    } catch (err) {
      setSettingsMsg({
        type: "err",
        text: err instanceof ApiError ? err.message : "Falha ao atualizar taxa.",
      });
    } finally {
      setSavingSettings(false);
    }
  }

  // Estorno de venda rápida (Admin)
  async function handleVoidSale(e: React.FormEvent) {
    e.preventDefault();
    if (!voidTarget || submittingVoid) return;
    setVoidError(null);
    if (voidReason.trim().length < 3) {
      setVoidError("Informe o motivo do estorno (mínimo 3 caracteres).");
      return;
    }
    setSubmittingVoid(true);
    try {
      await apiFetch(`/sales/${voidTarget.id}/void`, {
        method: "POST",
        body: JSON.stringify({
          reason: voidReason.trim(),
          customer_refund_completed: voidRefundCompleted,
          customer_refund_method: voidRefundCompleted ? voidRefundMethod : undefined,
          customer_refund_notes: voidRefundCompleted && voidRefundNotes.trim() ? voidRefundNotes.trim() : undefined,
        }),
      });
      setVoidTarget(null);
      setVoidReason("");
      setVoidRefundCompleted(false);
      setVoidRefundNotes("");
      await loadAll();
    } catch (err) {
      setVoidError(err instanceof ApiError ? err.message : "Não foi possível estornar a venda.");
    } finally {
      setSubmittingVoid(false);
    }
  }

  async function handleConfirmCustomerRefund(e: React.FormEvent) {
    e.preventDefault();
    if (!refundTarget || submittingRefund) return;
    setRefundError(null);
    setSubmittingRefund(true);
    try {
      await apiFetch(`/sales/${refundTarget.id}/customer-refund`, {
        method: "POST",
        body: JSON.stringify({
          method: refundMethod,
          notes: refundNotes.trim() || undefined,
        }),
      });
      setRefundTarget(null);
      setRefundNotes("");
      await loadAll();
    } catch (err) {
      setRefundError(err instanceof ApiError ? err.message : "Erro ao registrar devolução ao cliente.");
    } finally {
      setSubmittingRefund(false);
    }
  }

  if (state.status !== "authenticated") return null;
  const { user } = state;
  const isAdmin = user.role === "admin";

  return (
    <main className="container">
      <nav className="nav-tabs" aria-label="Módulos do caixa">
        <button
          type="button"
          className={`nav-tab ${activeTab === "pos" ? "active" : ""}`}
          onClick={() => setActiveTab("pos")}
        >
          Caixa (Venda Rápida)
        </button>
        <button
          type="button"
          className={`nav-tab ${activeTab === "history" ? "active" : ""}`}
          onClick={() => setActiveTab("history")}
        >
          Histórico e Estornos ({sales.length})
        </button>
        {isAdmin && (
          <>
            <button
              type="button"
              className={`nav-tab ${activeTab === "products" ? "active" : ""}`}
              onClick={() => setActiveTab("products")}
            >
              Produtos ({products.length})
            </button>
            <button
              type="button"
              className={`nav-tab ${activeTab === "settings" ? "active" : ""}`}
              onClick={() => setActiveTab("settings")}
            >
              Taxa de Cartão ({settings.card_fee_percent.replace(".", ",")}%)
            </button>
          </>
        )}
      </nav>

      {globalError && (
        <div className="alert alert-error" role="alert">
          {globalError}
        </div>
      )}

      {/* ==================== ABA 1: CAIXA / VENDA RÁPIDA ==================== */}
      {activeTab === "pos" && (
        <div className="pos-layout">
          {/* Coluna Esquerda: Catálogo de Produtos e Item Avulso */}
          <section className="card" aria-label="Catálogo de produtos">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "0.75rem", flexWrap: "wrap" }}>
              <h2 style={{ margin: 0 }}>Produtos disponíveis</h2>
              <span className="muted" style={{ fontSize: "0.85rem" }}>
                Taxa cartão atual: <strong>{settings.card_fee_percent.replace(".", ",")}%</strong>
              </span>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: "0.6rem", marginTop: "0.85rem" }}>
              <input
                type="search"
                className="input"
                placeholder="Pesquisar produto por nome…"
                value={posSearch}
                onChange={(e) => setPosSearch(e.target.value)}
                aria-label="Pesquisar produto por nome"
              />
              <select
                className="select"
                value={posCategory}
                onChange={(e) => setPosCategory(e.target.value)}
                aria-label="Filtrar por categoria"
              >
                <option value="all">Todas categorias</option>
                {categories.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>

            {loadingData ? (
              <p className="muted" style={{ marginTop: "1rem" }}>Carregando produtos…</p>
            ) : filteredPosProducts.length === 0 ? (
              <div style={{ marginTop: "1rem", padding: "1rem", border: "1px dashed var(--border)", borderRadius: 10 }}>
                <p className="muted" style={{ margin: 0 }}>
                  Nenhum produto ativo encontrado.{" "}
                  {isAdmin && (
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      style={{ marginLeft: "0.5rem" }}
                      onClick={() => setActiveTab("products")}
                    >
                      Cadastrar produtos
                    </button>
                  )}
                </p>
              </div>
            ) : (
              <div className="product-grid">
                {filteredPosProducts.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    className="product-btn"
                    onClick={() => addProductToCart(p)}
                  >
                    <span className="product-btn-cat">{p.category}</span>
                    <span className="product-btn-name">{p.name}</span>
                    <span className="product-btn-price">{formatBRL(p.price)}</span>
                  </button>
                ))}
              </div>
            )}

            {/* Adição de item avulso */}
            <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "1.25rem 0 1rem" }} />
            <h3 style={{ fontSize: "1rem" }}>Adicionar item avulso</h3>
            <form
              onSubmit={addCustomItemToCart}
              style={{ display: "grid", gridTemplateColumns: "1.6fr 1fr 0.6fr auto", gap: "0.5rem", alignItems: "end" }}
            >
              <div>
                <label className="muted" style={{ fontSize: "0.8rem", display: "block" }}>Descrição</label>
                <input
                  type="text"
                  className="input"
                  style={{ width: "100%" }}
                  placeholder="Ex.: Adicional especial"
                  value={customDesc}
                  onChange={(e) => setCustomDesc(e.target.value)}
                />
              </div>
              <div>
                <label className="muted" style={{ fontSize: "0.8rem", display: "block" }}>Valor unit. (R$)</label>
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
                <label className="muted" style={{ fontSize: "0.8rem", display: "block" }}>Qtd.</label>
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
              <button type="submit" className="btn btn-secondary">
                + Avulso
              </button>
            </form>
          </section>

          {/* Coluna Direita: Carrinho, Pagamentos, Taxas, Troco e Confirmação */}
          <section className="card" aria-label="Carrinho e pagamento">
            <h2 style={{ margin: 0 }}>Venda atual</h2>

            {confirmedSale && (
              <div className="alert alert-success" role="status" style={{ marginTop: "0.85rem" }}>
                <strong>Venda #{confirmedSale.id} confirmada com sucesso!</strong>
                <div style={{ fontSize: "0.9rem", marginTop: "0.25rem" }}>
                  Produtos: {formatBRL(confirmedSale.subtotal)}
                  {confirmedSale.card_fee_total !== "0.00" && ` · Taxa cartão: ${formatBRL(confirmedSale.card_fee_total)}`}
                  {" · "}Total cobrado: <strong>{formatBRL(confirmedSale.total_charged)}</strong>
                </div>
                {confirmedSale.payments.some((m) => m.change_given && m.change_given !== "0.00") && (
                  <div style={{ marginTop: "0.25rem", fontWeight: 700 }}>
                    Troco a devolver:{" "}
                    {formatBRL(
                      confirmedSale.payments.find((m) => m.change_given && m.change_given !== "0.00")
                        ?.change_given ?? "0.00",
                    )}
                  </div>
                )}
              </div>
            )}

            {saleError && (
              <div className="alert alert-error" role="alert" style={{ marginTop: "0.85rem" }}>
                {saleError}
              </div>
            )}

            {cart.length === 0 ? (
              <p className="muted" style={{ margin: "1rem 0" }}>
                Selecione produtos ao lado ou adicione um item avulso para iniciar a venda.
              </p>
            ) : (
              <div className="cart-list">
                {cart.map((item) => (
                  <div key={item.key} className="cart-item">
                    <div>
                      <div style={{ fontWeight: 700 }}>
                        {item.description}{" "}
                        {item.kind === "custom" && <span className="badge">Avulso</span>}
                      </div>
                      <div className="muted" style={{ fontSize: "0.82rem" }}>
                        {formatBRL(item.unitPriceCents)} × {item.quantity} ={" "}
                        <strong>{formatBRL(item.unitPriceCents * item.quantity)}</strong>
                      </div>
                    </div>
                    <div className="qty-controls">
                      <button
                        type="button"
                        className="qty-btn"
                        aria-label={`Diminuir quantidade de ${item.description}`}
                        onClick={() => updateCartQuantity(item.key, -1)}
                      >
                        −
                      </button>
                      <span style={{ minWidth: 24, textAlign: "center", fontWeight: 700 }}>
                        {item.quantity}
                      </span>
                      <button
                        type="button"
                        className="qty-btn"
                        aria-label={`Aumentar quantidade de ${item.description}`}
                        onClick={() => updateCartQuantity(item.key, 1)}
                      >
                        +
                      </button>
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        style={{ marginLeft: "0.25rem", color: "var(--danger)" }}
                        onClick={() => removeCartItem(item.key)}
                      >
                        Remover
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Formas de pagamento */}
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: "0.75rem", marginBottom: "0.5rem" }}>
              <h3 style={{ fontSize: "1rem", margin: 0 }}>Formas de pagamento</h3>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={addPaymentLine}
                disabled={cart.length === 0}
              >
                + Dividir pagamento
              </button>
            </div>

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
                        updatePaymentLine(p.id, {
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
                      Parcela dos produtos (R$)
                    </label>
                    <input
                      type="text"
                      inputMode="decimal"
                      className="input"
                      style={{ width: "100%" }}
                      placeholder="0,00"
                      value={p.amountInput}
                      onChange={(e) => updatePaymentLine(p.id, { amountInput: e.target.value })}
                    />
                  </div>

                  <div style={{ display: "flex", gap: "0.35rem" }}>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      title="Preencher com o saldo restante dos produtos"
                      onClick={() => fillRemainingInLine(p.id)}
                    >
                      Restante
                    </button>
                    {computedPayments.length > 1 && (
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        style={{ color: "var(--danger)" }}
                        onClick={() => removePaymentLine(p.id)}
                      >
                        ✕
                      </button>
                    )}
                  </div>
                </div>

                {p.method === "card" && p.amountCents > 0 && (
                  <div className="muted" style={{ fontSize: "0.83rem", marginTop: "0.45rem" }}>
                    Principal: <strong>{formatBRL(p.amountCents)}</strong> · Taxa (
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
                          updatePaymentLine(p.id, { cashTenderedInput: e.target.value })
                        }
                      />
                    </div>
                    <div style={{ fontSize: "0.9rem" }}>
                      {p.changeCents !== null && p.changeCents >= 0 ? (
                        <div>
                          Troco desta parcela:{" "}
                          <strong style={{ color: "var(--success)", fontSize: "1.05rem" }}>
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

            {/* Resumo sempre visível de Subtotal, Taxa, Total e Troco */}
            <div className="totals-box" aria-live="polite">
              <div className="totals-row">
                <span>Subtotal dos produtos:</span>
                <strong>{formatBRL(subtotalCents)}</strong>
              </div>
              <div className="totals-row">
                <span>Taxa de cartão ({settings.card_fee_percent.replace(".", ",")}%):</span>
                <strong>{formatBRL(totalCardFeeCents)}</strong>
              </div>
              <div className="totals-row">
                <span>Soma das parcelas (produtos):</span>
                <strong
                  style={{
                    color:
                      remainingPrincipalCents === 0 && subtotalCents > 0
                        ? "var(--success)"
                        : "var(--warning)",
                  }}
                >
                  {formatBRL(totalPrincipalAllocatedCents)}
                  {subtotalCents > 0 &&
                    remainingPrincipalCents !== 0 &&
                    ` (${remainingPrincipalCents > 0 ? `faltam ${formatBRL(remainingPrincipalCents)}` : `excede ${formatBRL(-remainingPrincipalCents)}`})`}
                </strong>
              </div>
              <div className="totals-row grand">
                <span>Total final cobrado:</span>
                <span id="pos-grand-total">{formatBRL(totalChargedCents)}</span>
              </div>
            </div>

            {totalChangeCents > 0 && (
              <div className="change-banner">
                <span>Troco a devolver ao cliente:</span>
                <span>{formatBRL(totalChangeCents)}</span>
              </div>
            )}

            <div style={{ display: "flex", gap: "0.6rem", marginTop: "0.75rem" }}>
              <button
                type="button"
                className="btn"
                style={{ flex: 1 }}
                disabled={cart.length === 0 || submittingSale}
                onClick={openSaleConfirmationModal}
              >
                Revisar e Confirmar Venda
              </button>
              {cart.length > 0 && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={submittingSale}
                  onClick={() => {
                    setCart([]);
                    setPayments([{ id: "pay-1", method: "pix", amountInput: "", cashTenderedInput: "" }]);
                    regenerateIdempotencyKey();
                  }}
                >
                  Limpar
                </button>
              )}
            </div>
          </section>
        </div>
      )}

      {/* ==================== ABA 2: HISTÓRICO E ESTORNOS ==================== */}
      {activeTab === "history" && (
        <section className="card" aria-label="Histórico de vendas e resumo financeiro">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "0.75rem" }}>
            <h2 style={{ margin: 0 }}>Resumo financeiro e histórico de vendas</h2>
            <button type="button" className="btn btn-secondary btn-sm" onClick={loadAll}>
              Atualizar
            </button>
          </div>

          {summary && (
            <div className="grid" style={{ marginBottom: "1.25rem" }}>
              <div className="card" style={{ padding: "1rem" }}>
                <div className="stat-label">Vendas ativas (produtos)</div>
                <div className="stat-value">
                  {formatBRL(summary.active_sales_subtotal)} ({summary.active_sales_count})
                </div>
              </div>
              <div className="card" style={{ padding: "1rem" }}>
                <div className="stat-label">Taxas de cartão repassadas</div>
                <div className="stat-value">{formatBRL(summary.active_card_fees)}</div>
              </div>
              <div className="card" style={{ padding: "1rem" }}>
                <div className="stat-label">Recebimento contábil reconhecido</div>
                <div style={{ fontSize: "0.86rem", marginTop: "0.25rem" }}>
                  <div>Pix: <strong>{formatBRL(summary.by_method.pix.recognized_gross)}</strong> (caixa: {formatBRL(summary.by_method.pix.net_gross)})</div>
                  <div>
                    Cartão: <strong>{formatBRL(summary.by_method.card.recognized_gross)}</strong> (Principal{" "}
                    {formatBRL(summary.by_method.card.recognized_principal)} + Taxa{" "}
                    {formatBRL(summary.by_method.card.recognized_fee)})
                  </div>
                  <div>Dinheiro: <strong>{formatBRL(summary.by_method.cash.recognized_gross)}</strong> (físico: {formatBRL(summary.by_method.cash.net_gross)})</div>
                </div>
              </div>
              <div className="card" style={{ padding: "1rem" }}>
                <div className="stat-label">Estornos / Devoluções ao cliente</div>
                <div className="stat-value">
                  {summary.voided_sales_count} estornada(s) ({formatBRL(summary.voided_total_charged)})
                </div>
                {summary.pending_customer_refunds_count > 0 ? (
                  <div style={{ marginTop: "0.3rem" }}>
                    <span className="badge badge-warning">
                      {summary.pending_customer_refunds_count} pendente(s): {formatBRL(summary.pending_customer_refunds_total)}
                    </span>
                  </div>
                ) : (
                  <div className="muted" style={{ fontSize: "0.82rem", marginTop: "0.25rem" }}>
                    Devolvido: {formatBRL(summary.completed_customer_refunds_total)}
                  </div>
                )}
              </div>
            </div>
          )}

          {sales.length === 0 ? (
            <p className="muted">Nenhuma venda registrada até o momento.</p>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Status</th>
                    <th>Itens</th>
                    <th>Subtotal</th>
                    <th>Taxa Cartão</th>
                    <th>Total Cobrado</th>
                    <th>Pagamentos / Troco</th>
                    <th>Devolução Cliente</th>
                    {isAdmin && <th>Ações</th>}
                  </tr>
                </thead>
                <tbody>
                  {sales.map((s) => {
                    const activePays = s.payments.filter((m) => m.kind === "sale_payment");
                    return (
                      <tr key={s.id}>
                        <td>
                          <strong>#{s.id}</strong>
                        </td>
                        <td>
                          {s.status === "active" ? (
                            <span className="badge badge-success">Ativa</span>
                          ) : (
                            <div>
                              <span className="badge badge-danger">Estornada</span>
                              <div className="muted" style={{ fontSize: "0.78rem", marginTop: "0.2rem" }}>
                                Motivo: {s.void_reason}
                              </div>
                            </div>
                          )}
                        </td>
                        <td>
                          {s.items.map((it) => (
                            <div key={it.id} style={{ fontSize: "0.86rem" }}>
                              {it.quantity}× {it.description} ({formatBRL(it.line_total)})
                            </div>
                          ))}
                        </td>
                        <td>{formatBRL(s.subtotal)}</td>
                        <td>{formatBRL(s.card_fee_total)}</td>
                        <td>
                          <strong>{formatBRL(s.total_charged)}</strong>
                        </td>
                        <td>
                          {activePays.map((m) => (
                            <div key={m.id} style={{ fontSize: "0.84rem" }}>
                              <strong>{methodLabel(m.method)}</strong>: {formatBRL(m.gross_amount)}
                              {m.fee_amount !== "0.00" && ` (taxa ${formatBRL(m.fee_amount)})`}
                              {m.cash_tendered && (
                                <span className="muted">
                                  {" "}
                                  [entregue {formatBRL(m.cash_tendered)}, troco{" "}
                                  {formatBRL(m.change_given ?? "0.00")}]
                                </span>
                              )}
                            </div>
                          ))}
                        </td>
                        <td>
                          {s.status === "active" ? (
                            <span className="muted">—</span>
                          ) : s.customer_refund_status === "completed" ? (
                            <div>
                              <span className="badge badge-success">Devolvido ao cliente</span>
                              {s.customer_refund_method && (
                                <div className="muted" style={{ fontSize: "0.78rem" }}>
                                  via {methodLabel(s.customer_refund_method)}
                                  {s.customer_refund_notes ? ` (${s.customer_refund_notes})` : ""}
                                </div>
                              )}
                            </div>
                          ) : (
                            <span className="badge badge-warning">Devolução pendente</span>
                          )}
                        </td>
                        {isAdmin && (
                          <td>
                            {s.status === "active" && (
                              <button
                                type="button"
                                className="btn btn-danger btn-sm"
                                onClick={() => {
                                  setVoidTarget(s);
                                  setVoidReason("");
                                  setVoidRefundCompleted(false);
                                  setVoidRefundNotes("");
                                  setVoidError(null);
                                }}
                              >
                                Estornar
                              </button>
                            )}
                            {s.status === "voided" && s.customer_refund_status === "pending" && (
                              <button
                                type="button"
                                className="btn btn-secondary btn-sm"
                                onClick={() => {
                                  setRefundTarget(s);
                                  setRefundMethod("pix");
                                  setRefundNotes("");
                                  setRefundError(null);
                                }}
                              >
                                Confirmar devolução
                              </button>
                            )}
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {/* ==================== ABA 3: GERENCIAMENTO DE PRODUTOS (ADMIN) ==================== */}
      {activeTab === "products" && isAdmin && (
        <div className="pos-layout">
          <section className="card" aria-label="Formulário de produto">
            <h2>{editingProduct ? `Editar produto #${editingProduct.id}` : "Novo produto"}</h2>
            {productMsg && (
              <div className={`alert ${productMsg.type === "ok" ? "alert-success" : "alert-error"}`}>
                {productMsg.text}
              </div>
            )}
            <form onSubmit={handleSaveProduct}>
              <div className="field">
                <label htmlFor="prod-name">Nome do produto</label>
                <input
                  id="prod-name"
                  type="text"
                  className="input"
                  required
                  value={prodName}
                  onChange={(e) => setProdName(e.target.value)}
                  placeholder="Ex.: Açaí Tradicional 500ml"
                />
              </div>
              <div className="field">
                <label htmlFor="prod-cat">Categoria</label>
                <input
                  id="prod-cat"
                  type="text"
                  className="input"
                  value={prodCategory}
                  onChange={(e) => setProdCategory(e.target.value)}
                  placeholder="Ex.: Açaí, Bebidas, Adicionais"
                />
              </div>
              <div className="field">
                <label htmlFor="prod-price">Preço unitário (R$)</label>
                <input
                  id="prod-price"
                  type="text"
                  inputMode="decimal"
                  className="input"
                  required
                  value={prodPrice}
                  onChange={(e) => setProdPrice(e.target.value)}
                  placeholder="Ex.: 18,50"
                />
              </div>
              <div style={{ display: "flex", gap: "0.5rem" }}>
                <button type="submit" className="btn" disabled={savingProduct}>
                  {savingProduct
                    ? "Salvando…"
                    : editingProduct
                      ? "Salvar alterações"
                      : "Cadastrar produto"}
                </button>
                {editingProduct && (
                  <button type="button" className="btn btn-secondary" onClick={resetProductForm}>
                    Cancelar edição
                  </button>
                )}
              </div>
            </form>
          </section>

          <section className="card" aria-label="Lista de produtos cadastrados">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "0.75rem", flexWrap: "wrap", marginBottom: "0.75rem" }}>
              <h2 style={{ margin: 0 }}>Produtos cadastrados</h2>
              <input
                type="search"
                className="input"
                placeholder="Pesquisar por nome…"
                value={adminSearch}
                onChange={(e) => setAdminSearch(e.target.value)}
              />
            </div>
            <p className="muted" style={{ fontSize: "0.85rem", marginTop: 0 }}>
              Alterações de preço valem apenas para novas vendas. Produtos desativados não aparecem na tela de vendas, mas preservam todo o histórico financeiro.
            </p>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Nome</th>
                    <th>Categoria</th>
                    <th>Preço</th>
                    <th>Status</th>
                    <th>Ações</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredAdminProducts.map((p) => (
                    <tr key={p.id}>
                      <td>
                        <strong>{p.name}</strong>
                      </td>
                      <td>{p.category}</td>
                      <td>{formatBRL(p.price)}</td>
                      <td>
                        <span className={`badge ${p.is_active ? "badge-success" : "badge-danger"}`}>
                          {p.is_active ? "Ativo" : "Inativo"}
                        </span>
                      </td>
                      <td style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
                        <button
                          type="button"
                          className="btn btn-secondary btn-sm"
                          onClick={() => startEditProduct(p)}
                        >
                          Editar
                        </button>
                        <button
                          type="button"
                          className="btn btn-secondary btn-sm"
                          onClick={() => toggleProductActive(p)}
                        >
                          {p.is_active ? "Desativar" : "Ativar"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}

      {/* ==================== ABA 4: CONFIGURAÇÃO DE TAXA DE CARTÃO (ADMIN) ==================== */}
      {activeTab === "settings" && isAdmin && (
        <section className="card" style={{ maxWidth: 640 }} aria-label="Configuração de taxa de cartão">
          <h2>Configuração da taxa de cartão</h2>
          <p className="muted">
            A taxa do cartão é repassada integralmente ao cliente e incide <strong>somente sobre a parcela paga no cartão</strong> (Pix e dinheiro não sofrem acréscimo). Mudanças nesta tela afetam apenas vendas futuras; o histórico anterior permanece inalterado.
          </p>

          {settingsMsg && (
            <div className={`alert ${settingsMsg.type === "ok" ? "alert-success" : "alert-error"}`}>
              {settingsMsg.text}
            </div>
          )}

          <form onSubmit={handleSaveSettings}>
            <div className="field">
              <label htmlFor="fee-percent">Percentual da taxa do cartão (%)</label>
              <input
                id="fee-percent"
                type="text"
                inputMode="decimal"
                className="input"
                value={feePercentInput}
                onChange={(e) => setFeePercentInput(e.target.value)}
                placeholder="0.00"
              />
            </div>

            <div className="field">
              <label htmlFor="fee-mode">Modo de cálculo da taxa</label>
              <select
                id="fee-mode"
                className="select"
                value={feeModeInput}
                onChange={(e) => setFeeModeInput(e.target.value as CardFeeMode)}
              >
                <option value="simple_addition">
                  Acréscimo simples sobre a parcela — Taxa = Principal × (Taxa% / 100) (Padrão MVP)
                </option>
                <option value="pass_through">
                  Repasse por dentro (desconto no bruto) — Bruto = Principal / (1 − Taxa% / 100)
                </option>
              </select>
            </div>

            <button type="submit" className="btn" disabled={savingSettings}>
              {savingSettings ? "Salvando…" : "Salvar configuração de taxa"}
            </button>
          </form>
        </section>
      )}

      {/* ==================== MODAL: CONFIRMAÇÃO DE VENDA RÁPIDA ==================== */}
      {reviewModalOpen && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="confirm-sale-title">
          <div className="modal-card">
            <h2 id="confirm-sale-title">Confirmar venda rápida</h2>
            <p className="muted" style={{ marginTop: 0 }}>
              Confira os itens, formas de pagamento, taxa e troco antes de registrar.
            </p>

            <div style={{ marginBottom: "0.85rem" }}>
              <strong>Itens ({cart.length}):</strong>
              {cart.map((it) => (
                <div key={it.key} style={{ display: "flex", justifyContent: "space-between", fontSize: "0.92rem", padding: "0.2rem 0" }}>
                  <span>
                    {it.quantity}× {it.description}
                  </span>
                  <strong>{formatBRL(it.unitPriceCents * it.quantity)}</strong>
                </div>
              ))}
            </div>

            <div style={{ marginBottom: "0.85rem" }}>
              <strong>Pagamentos:</strong>
              {computedPayments.map((p, i) => (
                <div key={p.id} style={{ fontSize: "0.92rem", padding: "0.25rem 0" }}>
                  <div>
                    #{i + 1} <strong>{methodLabel(p.method)}</strong>: {formatBRL(p.grossCents)}
                    {p.feeCents > 0 && ` (Principal ${formatBRL(p.amountCents)} + Taxa ${formatBRL(p.feeCents)})`}
                  </div>
                  {p.method === "cash" && p.tenderedCents !== null && (
                    <div className="muted" style={{ fontSize: "0.84rem" }}>
                      Entregue: {formatBRL(p.tenderedCents)} · Troco: <strong>{formatBRL(p.changeCents ?? 0)}</strong>
                    </div>
                  )}
                </div>
              ))}
            </div>

            <div className="totals-box">
              <div className="totals-row">
                <span>Subtotal dos produtos:</span>
                <strong>{formatBRL(subtotalCents)}</strong>
              </div>
              <div className="totals-row">
                <span>Taxa de cartão:</span>
                <strong>{formatBRL(totalCardFeeCents)}</strong>
              </div>
              <div className="totals-row grand">
                <span>Total a receber:</span>
                <span>{formatBRL(totalChargedCents)}</span>
              </div>
            </div>

            {totalChangeCents > 0 && (
              <div className="change-banner">
                <span>Troco a devolver:</span>
                <span>{formatBRL(totalChangeCents)}</span>
              </div>
            )}

            {saleError && <div className="alert alert-error">{saleError}</div>}

            <div style={{ display: "flex", gap: "0.6rem", justifyContent: "flex-end", marginTop: "1rem" }}>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={submittingSale}
                onClick={() => setReviewModalOpen(false)}
              >
                Voltar
              </button>
              <button
                type="button"
                className="btn btn-success"
                disabled={submittingSale}
                onClick={submitQuickSale}
              >
                {submittingSale ? "Registrando venda…" : "Confirmar e Registrar"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ==================== MODAL: ESTORNAR VENDA RÁPIDA ==================== */}
      {voidTarget && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="void-sale-title">
          <div className="modal-card">
            <h2 id="void-sale-title">Estornar venda #{voidTarget.id}</h2>
            <p className="muted">
              O estorno cancela contabilmente a venda e reverte o recebimento reconhecido no turno, preservando todo o histórico original.
            </p>
            <div className="alert alert-warning" style={{ fontSize: "0.88rem" }}>
              <strong>Atenção:</strong> estornar a venda no sistema <strong>não</strong> significa que o dinheiro foi devolvido automaticamente ao cliente. Informe abaixo se a devolução financeira ao cliente já ocorreu ou se ficará pendente.
            </div>

            {voidError && <div className="alert alert-error">{voidError}</div>}

            <form onSubmit={handleVoidSale}>
              <div className="field">
                <label htmlFor="void-reason">Motivo obrigatório do estorno</label>
                <input
                  id="void-reason"
                  type="text"
                  className="input"
                  required
                  placeholder="Ex.: Erro de lançamento / desistência do cliente"
                  value={voidReason}
                  onChange={(e) => setVoidReason(e.target.value)}
                />
              </div>

              <div className="field" style={{ flexDirection: "row", alignItems: "center", gap: "0.5rem" }}>
                <input
                  id="void-refund-done"
                  type="checkbox"
                  checked={voidRefundCompleted}
                  onChange={(e) => setVoidRefundCompleted(e.target.checked)}
                />
                <label htmlFor="void-refund-done" style={{ margin: 0 }}>
                  O valor ({formatBRL(voidTarget.total_charged)}) já foi efetivamente devolvido ao cliente
                </label>
              </div>

              {voidRefundCompleted && (
                <>
                  <div className="field">
                    <label htmlFor="void-refund-method">Meio utilizado na devolução ao cliente</label>
                    <select
                      id="void-refund-method"
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
                    <label htmlFor="void-refund-notes">Observação da devolução (opcional)</label>
                    <input
                      id="void-refund-notes"
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
                  onClick={() => setVoidTarget(null)}
                >
                  Cancelar
                </button>
                <button type="submit" className="btn btn-danger" disabled={submittingVoid}>
                  {submittingVoid ? "Estornando…" : "Confirmar Estorno"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* ==================== MODAL: CONFIRMAR DEVOLUÇÃO EFETIVA AO CLIENTE ==================== */}
      {refundTarget && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="refund-sale-title">
          <div className="modal-card">
            <h2 id="refund-sale-title">Confirmar devolução ao cliente — Venda #{refundTarget.id}</h2>
            <p className="muted">
              Registre que o valor de <strong>{formatBRL(refundTarget.total_charged)}</strong> referente à venda já estornada foi efetivamente devolvido ao cliente.
            </p>
            {refundError && <div className="alert alert-error">{refundError}</div>}

            <form onSubmit={handleConfirmCustomerRefund}>
              <div className="field">
                <label htmlFor="refund-method">Meio utilizado para devolver ao cliente</label>
                <select
                  id="refund-method"
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
                <label htmlFor="refund-notes">Observação (opcional)</label>
                <input
                  id="refund-notes"
                  type="text"
                  className="input"
                  value={refundNotes}
                  onChange={(e) => setRefundNotes(e.target.value)}
                  placeholder="Ex.: Comprovante Pix enviado"
                />
              </div>
              <div style={{ display: "flex", gap: "0.6rem", justifyContent: "flex-end", marginTop: "1rem" }}>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={submittingRefund}
                  onClick={() => setRefundTarget(null)}
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
    </main>
  );
}
