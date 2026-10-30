"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, apiFetch } from "@/lib/api";
import {
  CurrentShiftState,
  DiscrepancyStatus,
  ShiftReport,
  centsToApiDecimal,
  formatBRL,
  generateIdempotencyKey,
  parseToCents,
} from "@/lib/money";

interface ShiftManagerProps {
  shiftState: CurrentShiftState | null;
  onShiftStateChanged: (nextState?: CurrentShiftState) => Promise<void> | void;
  onNavigateTab?: (tab: "pos" | "tabs" | "history") => void;
}

function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("pt-BR", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function diffBadge(diffCents: number | null, status?: DiscrepancyStatus) {
  if (diffCents === null || status === "pending") {
    return <span className="badge">Aguardando contagem</span>;
  }
  if (diffCents === 0 || status === "exact") {
    return <span className="badge badge-success">Conferência exata (R$ 0,00)</span>;
  }
  if (diffCents > 0 || status === "surplus") {
    return (
      <span className="badge badge-warning">
        Sobra (+{formatBRL(diffCents)})
      </span>
    );
  }
  return (
    <span className="badge badge-danger">
      Falta ({formatBRL(diffCents)})
    </span>
  );
}

export default function ShiftManager({
  shiftState,
  onShiftStateChanged,
  onNavigateTab,
}: ShiftManagerProps) {
  const [feedback, setFeedback] = useState<{
    type: "success" | "error" | "warning";
    text: string;
  } | null>(null);

  // Abertura de caixa
  const [openingCashInput, setOpeningCashInput] = useState("150,00");
  const [openingNotesInput, setOpeningNotesInput] = useState("");
  const [openingBusy, setOpeningBusy] = useState(false);

  // Sangria e Suprimento
  const [drawerKind, setDrawerKind] = useState<"withdrawal" | "supply">("withdrawal");
  const [drawerAmountInput, setDrawerAmountInput] = useState("");
  const [drawerReasonInput, setDrawerReasonInput] = useState("");
  const [drawerBusy, setDrawerBusy] = useState(false);

  // Conferência e Fechamento
  const [conferenceOpen, setConferenceOpen] = useState(false);
  const [countedCashInput, setCountedCashInput] = useState("");
  const [countedPixInput, setCountedPixInput] = useState("");
  const [countedCardInput, setCountedCardInput] = useState("");
  const [justificationInput, setJustificationInput] = useState("");
  const [closingNotesInput, setClosingNotesInput] = useState("");
  const [previewReport, setPreviewReport] = useState<ShiftReport | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [confirmCloseStep, setConfirmCloseStep] = useState(false);
  const [closingBusy, setClosingBusy] = useState(false);

  // Relatório Histórico
  const [selectedReport, setSelectedReport] = useState<ShiftReport | null>(null);
  const [reportLoadingId, setReportLoadingId] = useState<number | null>(null);

  const isOpen = Boolean(shiftState?.is_open && shiftState?.current_shift);
  const currentShift = shiftState?.current_shift ?? null;
  const summary = shiftState?.summary ?? null;
  const conference = shiftState?.conference ?? null;
  const pendencies = shiftState?.pendencies ?? null;
  const drawerMovements = shiftState?.drawer_movements ?? [];
  const closedShifts = shiftState?.closed_shifts ?? [];

  const startConference = useCallback(() => {
    if (!conference) return;
    setCountedCashInput(conference.cash.expected_amount.replace(".", ","));
    setCountedPixInput(conference.pix.expected_amount.replace(".", ","));
    setCountedCardInput(conference.card.expected_amount.replace(".", ","));
    setPreviewReport(null);
    setConfirmCloseStep(false);
    setConferenceOpen(true);
  }, [conference]);

  useEffect(() => {
    if (!isOpen) {
      setConferenceOpen(false);
      setPreviewReport(null);
      setConfirmCloseStep(false);
    }
  }, [isOpen]);

  // Cálculos em tempo real das diferenças na conferência
  const liveDiffs = useMemo(() => {
    if (!conference) {
      return {
        cashExpectedCents: 0,
        cashCountedCents: null as number | null,
        cashDiffCents: null as number | null,
        pixExpectedCents: 0,
        pixCountedCents: null as number | null,
        pixDiffCents: null as number | null,
        cardExpectedCents: 0,
        cardCountedCents: null as number | null,
        cardDiffCents: null as number | null,
        hasDiscrepancy: false,
      };
    }
    const cashExpectedCents = parseToCents(conference.cash.expected_amount) ?? 0;
    const pixExpectedCents = parseToCents(conference.pix.expected_amount) ?? 0;
    const cardExpectedCents = parseToCents(conference.card.expected_amount) ?? 0;

    const cashCountedCents = countedCashInput.trim() ? parseToCents(countedCashInput) : null;
    const pixCountedCents = countedPixInput.trim() ? parseToCents(countedPixInput) : pixExpectedCents;
    const cardCountedCents = countedCardInput.trim() ? parseToCents(countedCardInput) : cardExpectedCents;

    const cashDiffCents = cashCountedCents !== null ? cashCountedCents - cashExpectedCents : null;
    const pixDiffCents = pixCountedCents !== null ? pixCountedCents - pixExpectedCents : null;
    const cardDiffCents = cardCountedCents !== null ? cardCountedCents - cardExpectedCents : null;

    const hasDiscrepancy =
      (cashDiffCents !== null && cashDiffCents !== 0) ||
      (pixDiffCents !== null && pixDiffCents !== 0) ||
      (cardDiffCents !== null && cardDiffCents !== 0);

    return {
      cashExpectedCents,
      cashCountedCents,
      cashDiffCents,
      pixExpectedCents,
      pixCountedCents,
      pixDiffCents,
      cardExpectedCents,
      cardCountedCents,
      cardDiffCents,
      hasDiscrepancy,
    };
  }, [conference, countedCashInput, countedPixInput, countedCardInput]);

  async function handleOpenShift(e: FormEvent) {
    e.preventDefault();
    setFeedback(null);
    const cents = parseToCents(openingCashInput);
    if (cents === null || cents < 0) {
      setFeedback({
        type: "error",
        text: "Informe um valor de fundo inicial válido (maior ou igual a R$ 0,00).",
      });
      return;
    }

    setOpeningBusy(true);
    try {
      const res = await apiFetch<{
        shift: CurrentShiftState["current_shift"];
        state: CurrentShiftState;
      }>("/shifts/open", {
        method: "POST",
        body: JSON.stringify({
          opening_cash: centsToApiDecimal(cents),
          notes: openingNotesInput.trim() || null,
          idempotency_key: generateIdempotencyKey(),
        }),
      });
      setOpeningNotesInput("");
      setSelectedReport(null);
      setFeedback({
        type: "success",
        text: `Turno #${res.shift?.id} aberto com sucesso! Fundo inicial: ${formatBRL(cents)}.`,
      });
      await onShiftStateChanged(res.state);
    } catch (err) {
      setFeedback({
        type: "error",
        text: err instanceof ApiError ? err.message : "Erro ao abrir o caixa.",
      });
    } finally {
      setOpeningBusy(false);
    }
  }

  async function handleDrawerMovement(e: FormEvent) {
    e.preventDefault();
    setFeedback(null);
    const cents = parseToCents(drawerAmountInput);
    if (cents === null || cents <= 0) {
      setFeedback({
        type: "error",
        text: "Informe um valor maior que R$ 0,00 para registrar a movimentação.",
      });
      return;
    }
    if (drawerReasonInput.trim().length < 3) {
      setFeedback({
        type: "error",
        text: "Informe o motivo da movimentação (mínimo de 3 caracteres).",
      });
      return;
    }

    setDrawerBusy(true);
    const endpoint =
      drawerKind === "withdrawal" ? "/shifts/withdrawals" : "/shifts/supplies";
    const label = drawerKind === "withdrawal" ? "Sangria" : "Suprimento";
    try {
      const res = await apiFetch<{ state: CurrentShiftState }>(endpoint, {
        method: "POST",
        body: JSON.stringify({
          amount: centsToApiDecimal(cents),
          reason: drawerReasonInput.trim(),
          idempotency_key: generateIdempotencyKey(),
        }),
      });
      setDrawerAmountInput("");
      setDrawerReasonInput("");
      setFeedback({
        type: "success",
        text: `${label} de ${formatBRL(cents)} registrado com sucesso.`,
      });
      await onShiftStateChanged(res.state);
    } catch (err) {
      setFeedback({
        type: "error",
        text: err instanceof ApiError ? err.message : `Erro ao registrar ${label.toLowerCase()}.`,
      });
    } finally {
      setDrawerBusy(false);
    }
  }

  async function handlePreviewClosing() {
    setFeedback(null);
    const cashCents = parseToCents(countedCashInput);
    if (cashCents === null) {
      setFeedback({
        type: "error",
        text: "Informe o valor contado em dinheiro na gaveta.",
      });
      return;
    }
    const pixCents = countedPixInput.trim() ? parseToCents(countedPixInput) : liveDiffs.pixExpectedCents;
    const cardCents = countedCardInput.trim() ? parseToCents(countedCardInput) : liveDiffs.cardExpectedCents;
    if (pixCents === null || cardCents === null) {
      setFeedback({
        type: "error",
        text: "Verifique os valores conferidos em Pix e Cartão.",
      });
      return;
    }

    setPreviewBusy(true);
    try {
      const res = await apiFetch<{ preview: ShiftReport }>("/shifts/preview-closing", {
        method: "POST",
        body: JSON.stringify({
          counted_cash: centsToApiDecimal(cashCents),
          counted_pix: centsToApiDecimal(pixCents),
          counted_card: centsToApiDecimal(cardCents),
          discrepancy_justification: justificationInput.trim() || null,
        }),
      });
      setPreviewReport(res.preview);
      setConfirmCloseStep(Boolean(res.preview.ready_to_close));
    } catch (err) {
      setFeedback({
        type: "error",
        text: err instanceof ApiError ? err.message : "Não foi possível gerar a prévia do fechamento.",
      });
    } finally {
      setPreviewBusy(false);
    }
  }

  async function handleConfirmCloseShift() {
    setFeedback(null);
    const cashCents = parseToCents(countedCashInput);
    if (cashCents === null) {
      setFeedback({
        type: "error",
        text: "Informe o valor contado em dinheiro para encerrar o turno.",
      });
      return;
    }
    const pixCents = countedPixInput.trim() ? parseToCents(countedPixInput) : liveDiffs.pixExpectedCents;
    const cardCents = countedCardInput.trim() ? parseToCents(countedCardInput) : liveDiffs.cardExpectedCents;
    if (pixCents === null || cardCents === null) {
      setFeedback({
        type: "error",
        text: "Valores conferidos em Pix ou Cartão inválidos.",
      });
      return;
    }
    if (liveDiffs.hasDiscrepancy && justificationInput.trim().length < 3) {
      setFeedback({
        type: "error",
        text: "Há divergência entre os valores esperados e conferidos. Informe a justificativa (mínimo de 3 caracteres).",
      });
      return;
    }

    setClosingBusy(true);
    try {
      const res = await apiFetch<{ shift: ShiftReport["shift"]; report: ShiftReport }>(
        "/shifts/close",
        {
          method: "POST",
          body: JSON.stringify({
            counted_cash: centsToApiDecimal(cashCents),
            counted_pix: centsToApiDecimal(pixCents),
            counted_card: centsToApiDecimal(cardCents),
            discrepancy_justification: justificationInput.trim() || null,
            notes: closingNotesInput.trim() || null,
            idempotency_key: generateIdempotencyKey(),
          }),
        },
      );
      setSelectedReport(res.report);
      setConferenceOpen(false);
      setPreviewReport(null);
      setConfirmCloseStep(false);
      setJustificationInput("");
      setClosingNotesInput("");
      setFeedback({
        type: "success",
        text: `Turno #${res.shift.id} encerrado com sucesso! Relatório de fechamento preservado abaixo.`,
      });
      await onShiftStateChanged();
    } catch (err) {
      setFeedback({
        type: "error",
        text: err instanceof ApiError ? err.message : "Erro ao encerrar o turno de caixa.",
      });
    } finally {
      setClosingBusy(false);
    }
  }

  async function handleViewHistoricalReport(shiftId: number) {
    setFeedback(null);
    setReportLoadingId(shiftId);
    try {
      const res = await apiFetch<{ report: ShiftReport }>(`/shifts/${shiftId}/report`);
      setSelectedReport(res.report);
    } catch (err) {
      setFeedback({
        type: "error",
        text: err instanceof ApiError ? err.message : "Erro ao carregar relatório do turno.",
      });
    } finally {
      setReportLoadingId(null);
    }
  }

  return (
    <div>
      {feedback && (
        <div
          className={`alert ${
            feedback.type === "error"
              ? "alert-error"
              : feedback.type === "warning"
                ? "alert-warning"
                : "alert-success"
          }`}
          role="status"
        >
          {feedback.text}
        </div>
      )}

      {/* 1. Cabeçalho de Estado Atual do Caixa */}
      <div
        className="card no-print"
        style={{
          marginBottom: "1.25rem",
          borderLeft: `6px solid ${isOpen ? "var(--success)" : "var(--danger)"}`,
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            flexWrap: "wrap",
            gap: "1rem",
          }}
        >
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "0.65rem", marginBottom: "0.3rem" }}>
              <h2 style={{ margin: 0 }}>
                {isOpen && currentShift
                  ? `Caixa Aberto — Turno #${currentShift.id}`
                  : "Caixa Fechado"}
              </h2>
              <span className={`badge ${isOpen ? "badge-success" : "badge-danger"}`}>
                {isOpen ? "ABERTO" : "FECHADO"}
              </span>
            </div>
            {isOpen && currentShift ? (
              <div className="muted" style={{ fontSize: "0.9rem" }}>
                Aberto em <strong>{formatDateTime(currentShift.opened_at)}</strong> por{" "}
                <strong>{currentShift.opened_by_name || `Operador #${currentShift.opened_by}`}</strong>{" "}
                · Fundo inicial em dinheiro: <strong>{formatBRL(currentShift.opening_cash)}</strong>
                {currentShift.notes ? ` · Obs.: ${currentShift.notes}` : ""}
              </div>
            ) : (
              <div className="muted" style={{ fontSize: "0.9rem" }}>
                Nenhuma venda, comanda, pagamento, depósito ou devolução pode ser realizada enquanto o caixa estiver fechado.
              </div>
            )}
          </div>

          {isOpen && (
            <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
              {!conferenceOpen ? (
                <button
                  type="button"
                  className="btn"
                  onClick={startConference}
                >
                  Iniciar Conferência e Fechamento
                </button>
              ) : (
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => {
                    setConferenceOpen(false);
                    setPreviewReport(null);
                    setConfirmCloseStep(false);
                  }}
                >
                  Ocultar Conferência
                </button>
              )}
            </div>
          )}
        </div>

        {/* Formulário de Abertura quando o caixa está fechado */}
        {!isOpen && (
          <form
            onSubmit={handleOpenShift}
            style={{
              marginTop: "1rem",
              paddingTop: "1rem",
              borderTop: "1px solid var(--border)",
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
              gap: "0.85rem",
              alignItems: "end",
            }}
          >
            <div className="field" style={{ marginBottom: 0 }}>
              <label htmlFor="shift-opening-cash">Fundo inicial disponível para troco (R$)</label>
              <input
                id="shift-opening-cash"
                className="input"
                inputMode="decimal"
                placeholder="Ex.: 150,00"
                value={openingCashInput}
                onChange={(e) => setOpeningCashInput(e.target.value)}
                required
              />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label htmlFor="shift-opening-notes">Observação de abertura (opcional)</label>
              <input
                id="shift-opening-notes"
                className="input"
                placeholder="Ex.: Turno abertura 17h Festival do Açaí"
                value={openingNotesInput}
                onChange={(e) => setOpeningNotesInput(e.target.value)}
                maxLength={255}
              />
            </div>
            <div>
              <button
                type="submit"
                className="btn btn-success"
                style={{ width: "100%" }}
                disabled={openingBusy}
              >
                {openingBusy ? "Abrindo Caixa..." : "Abrir Caixa"}
              </button>
            </div>
          </form>
        )}
      </div>

      {/* 2. Painel Operacional do Turno Aberto */}
      {isOpen && summary && conference && pendencies && (
        <div className="no-print">
          {/* Alertas de Pendências e Bloqueios de Fechamento */}
          {!pendencies.can_close && (
            <div className="alert alert-warning" style={{ marginBottom: "1.25rem" }}>
              <div style={{ fontWeight: 700, marginBottom: "0.35rem" }}>
                Pendências operacionais que impedem o fechamento do turno no momento:
              </div>
              <ul style={{ margin: "0 0 0.5rem 1.2rem", padding: 0 }}>
                {pendencies.blockers.map((b) => (
                  <li key={b.code} style={{ marginBottom: "0.2rem" }}>
                    {b.message}
                  </li>
                ))}
              </ul>
              {pendencies.open_tabs.length > 0 && (
                <div style={{ fontSize: "0.86rem", marginBottom: "0.45rem" }}>
                  <strong>Comandas abertas:</strong>{" "}
                  {pendencies.open_tabs.map((t) => (
                    <span
                      key={t.id}
                      className="badge badge-warning"
                      style={{ marginRight: "0.35rem" }}
                    >
                      #{t.number} {t.customer_name ? `(${t.customer_name})` : ""} · Consumo:{" "}
                      {formatBRL(t.subtotal_consumed)} · Falta pagar: {formatBRL(t.balance_due)} · Crédito disp.:{" "}
                      {formatBRL(t.credit_balance)}
                    </span>
                  ))}
                </div>
              )}
              {pendencies.pending_refunds.length > 0 && (
                <div style={{ fontSize: "0.86rem", marginBottom: "0.45rem" }}>
                  <strong>Devoluções pendentes ao cliente:</strong>{" "}
                  {pendencies.pending_refunds.map((r, idx) => (
                    <span
                      key={`${r.obligation_kind}-${r.sale_id ?? r.movement_id ?? idx}`}
                      className="badge badge-danger"
                      style={{ marginRight: "0.35rem" }}
                    >
                      {r.tab_number ? `Comanda #${r.tab_number}` : `Venda #${r.sale_id}`} · Valor a devolver:{" "}
                      {formatBRL(r.refund_amount)}
                    </span>
                  ))}
                </div>
              )}
              {onNavigateTab && (
                <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", marginTop: "0.5rem" }}>
                  {pendencies.open_tabs_count > 0 && (
                    <button
                      type="button"
                      className="btn btn-sm btn-secondary"
                      onClick={() => onNavigateTab("tabs")}
                    >
                      Ir para Comandas ({pendencies.open_tabs_count})
                    </button>
                  )}
                  {pendencies.pending_refunds_count > 0 && (
                    <button
                      type="button"
                      className="btn btn-sm btn-secondary"
                      onClick={() => onNavigateTab("history")}
                    >
                      Resolver Devoluções Pendentes ({pendencies.pending_refunds_count})
                    </button>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Indicadores Financeiros do Turno */}
          <div
            className="grid"
            style={{
              gridTemplateColumns: "repeat(auto-fit, minmax(215px, 1fr))",
              marginTop: 0,
              marginBottom: "1.25rem",
            }}
          >
            <div className="card" style={{ borderLeft: "4px solid var(--success)" }}>
              <div className="stat-label">Saldo Esperado em Dinheiro (Gaveta)</div>
              <div className="stat-value" style={{ color: "var(--success)", fontSize: "1.35rem" }}>
                {formatBRL(conference.cash.expected_amount)}
              </div>
              <div className="muted" style={{ fontSize: "0.78rem", marginTop: "0.2rem" }}>
                Fundo {formatBRL(conference.cash.opening_cash)} + Entradas {formatBRL(conference.cash.inflows)} − Saídas{" "}
                {formatBRL(conference.cash.outflows)} + Supr. {formatBRL(conference.cash.supplies)} − Sangr.{" "}
                {formatBRL(conference.cash.withdrawals)}
              </div>
            </div>

            <div className="card">
              <div className="stat-label">Vendas de Produtos no Turno</div>
              <div className="stat-value">{formatBRL(summary.active_sales_subtotal)}</div>
              <div className="muted" style={{ fontSize: "0.78rem", marginTop: "0.2rem" }}>
                Rápidas: {formatBRL(summary.quick_sales_subtotal ?? "0.00")} · Comandas:{" "}
                {formatBRL(summary.tabs_consumed_subtotal ?? "0.00")} (Fundo inicial não incluso)
              </div>
            </div>

            <div className="card">
              <div className="stat-label">Pix Líquido Registrado</div>
              <div className="stat-value">{formatBRL(conference.pix.expected_amount)}</div>
              <div className="muted" style={{ fontSize: "0.78rem", marginTop: "0.2rem" }}>
                Entradas: {formatBRL(conference.pix.inflows)} · Devoluções: {formatBRL(conference.pix.outflows)}
              </div>
            </div>

            <div className="card">
              <div className="stat-label">Cartão Bruto Registrado (Maquininha)</div>
              <div className="stat-value">{formatBRL(conference.card.expected_amount)}</div>
              <div className="muted" style={{ fontSize: "0.78rem", marginTop: "0.2rem" }}>
                Principal: {formatBRL(conference.card.net_principal_registered)} + Taxas repassadas:{" "}
                {formatBRL(conference.card.net_fee_registered)}
              </div>
            </div>

            <div className="card">
              <div className="stat-label">Créditos Antecipados (Passivo)</div>
              <div className="stat-value">{formatBRL(summary.tabs_credit_deposited_total ?? "0.00")}</div>
              <div className="muted" style={{ fontSize: "0.78rem", marginTop: "0.2rem" }}>
                Aplicado: {formatBRL(summary.tabs_credit_applied_total ?? "0.00")} · Devolvido:{" "}
                {formatBRL(summary.tabs_credit_refunded_total ?? "0.00")} · Pendente:{" "}
                <strong>{formatBRL(summary.tabs_credit_balance_total ?? "0.00")}</strong>
              </div>
            </div>

            <div className="card">
              <div className="stat-label">Comandas e Obrigações</div>
              <div className="stat-value">
                {pendencies.open_tabs_count} aberta(s) / {pendencies.closed_tabs_count} fechada(s)
              </div>
              <div className="muted" style={{ fontSize: "0.78rem", marginTop: "0.2rem" }}>
                A receber: {formatBRL(pendencies.unpaid_consumption_total)} · Devoluções pend.:{" "}
                {formatBRL(pendencies.pending_refunds_total)}
              </div>
            </div>
          </div>

          {/* Seção de Conferência por Método e Encerramento do Turno */}
          {conferenceOpen && (
            <div
              className="card"
              style={{
                marginBottom: "1.25rem",
                border: "2px solid var(--primary)",
                background: "#fcfaff",
              }}
            >
              <h3 style={{ marginBottom: "0.25rem" }}>
                Conferência de Fechamento por Método de Pagamento
              </h3>
              <p className="muted" style={{ marginTop: 0, fontSize: "0.9rem" }}>
                Confira os valores físicos na gaveta e nos relatórios do Pix e da maquininha. Caso exista diferença
                (sobra ou falta), informe a justificativa obrigatória. Nenhum lançamento fictício será criado.
              </p>

              <div
                className="grid"
                style={{
                  gridTemplateColumns: "repeat(auto-fit, minmax(290px, 1fr))",
                  marginTop: "0.75rem",
                  marginBottom: "1rem",
                }}
              >
                {/* 1. Dinheiro */}
                <div className="card" style={{ background: "#fff" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.5rem" }}>
                    <h3 style={{ margin: 0 }}>1. Dinheiro (Gaveta)</h3>
                    {diffBadge(liveDiffs.cashDiffCents)}
                  </div>
                  <div style={{ fontSize: "0.88rem", marginBottom: "0.75rem" }}>
                    <div className="totals-row">
                      <span>(+) Fundo inicial:</span>
                      <strong>{formatBRL(conference.cash.opening_cash)}</strong>
                    </div>
                    <div className="totals-row">
                      <span>(+) Entradas efetivas (vendas/depósitos líq. troco):</span>
                      <strong>{formatBRL(conference.cash.inflows)}</strong>
                    </div>
                    <div className="totals-row">
                      <span>(−) Saídas efetivas (devoluções/estornos):</span>
                      <strong>{formatBRL(conference.cash.outflows)}</strong>
                    </div>
                    <div className="totals-row">
                      <span>(+) Suprimentos ({conference.cash.supplies_count}):</span>
                      <strong>{formatBRL(conference.cash.supplies)}</strong>
                    </div>
                    <div className="totals-row">
                      <span>(−) Sangrias ({conference.cash.withdrawals_count}):</span>
                      <strong>{formatBRL(conference.cash.withdrawals)}</strong>
                    </div>
                    <div
                      className="totals-row"
                      style={{
                        borderTop: "1px solid var(--border)",
                        marginTop: "0.35rem",
                        paddingTop: "0.35rem",
                        fontSize: "1rem",
                      }}
                    >
                      <span><strong>Saldo esperado na gaveta:</strong></span>
                      <strong style={{ color: "var(--primary-dark)" }}>
                        {formatBRL(conference.cash.expected_amount)}
                      </strong>
                    </div>
                  </div>
                  <div className="field" style={{ marginBottom: "0.4rem" }}>
                    <label htmlFor="conf-counted-cash">Valor contado em dinheiro pelo operador (R$)</label>
                    <input
                      id="conf-counted-cash"
                      className="input"
                      inputMode="decimal"
                      placeholder="Ex.: 162,00"
                      value={countedCashInput}
                      onChange={(e) => {
                        setCountedCashInput(e.target.value);
                        setConfirmCloseStep(false);
                      }}
                    />
                  </div>
                  <div style={{ fontSize: "0.86rem", display: "flex", justifyContent: "space-between" }}>
                    <span className="muted">Diferença (Contado − Esperado):</span>
                    <strong>
                      {liveDiffs.cashDiffCents === null ? "—" : formatBRL(liveDiffs.cashDiffCents)}
                    </strong>
                  </div>
                </div>

                {/* 2. Pix */}
                <div className="card" style={{ background: "#fff" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.5rem" }}>
                    <h3 style={{ margin: 0 }}>2. Pix</h3>
                    {diffBadge(liveDiffs.pixDiffCents)}
                  </div>
                  <div style={{ fontSize: "0.88rem", marginBottom: "0.75rem" }}>
                    <div className="totals-row">
                      <span>(+) Entradas Pix (vendas + depósitos):</span>
                      <strong>{formatBRL(conference.pix.inflows)}</strong>
                    </div>
                    <div className="totals-row">
                      <span>(−) Devoluções efetivas em Pix:</span>
                      <strong>{formatBRL(conference.pix.outflows)}</strong>
                    </div>
                    <div
                      className="totals-row"
                      style={{
                        borderTop: "1px solid var(--border)",
                        marginTop: "0.35rem",
                        paddingTop: "0.35rem",
                        fontSize: "1rem",
                      }}
                    >
                      <span><strong>Total líquido Pix registrado:</strong></span>
                      <strong style={{ color: "var(--primary-dark)" }}>
                        {formatBRL(conference.pix.expected_amount)}
                      </strong>
                    </div>
                  </div>
                  <div className="field" style={{ marginBottom: "0.4rem" }}>
                    <label htmlFor="conf-counted-pix">Valor conferido no extrato Pix (R$)</label>
                    <input
                      id="conf-counted-pix"
                      className="input"
                      inputMode="decimal"
                      placeholder="0,00"
                      value={countedPixInput}
                      onChange={(e) => {
                        setCountedPixInput(e.target.value);
                        setConfirmCloseStep(false);
                      }}
                    />
                  </div>
                  <div style={{ fontSize: "0.86rem", display: "flex", justifyContent: "space-between" }}>
                    <span className="muted">Diferença (Conferido − Esperado):</span>
                    <strong>
                      {liveDiffs.pixDiffCents === null ? "—" : formatBRL(liveDiffs.pixDiffCents)}
                    </strong>
                  </div>
                </div>

                {/* 3. Cartão */}
                <div className="card" style={{ background: "#fff" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.5rem" }}>
                    <h3 style={{ margin: 0 }}>3. Cartão (Maquininha)</h3>
                    {diffBadge(liveDiffs.cardDiffCents)}
                  </div>
                  <div style={{ fontSize: "0.88rem", marginBottom: "0.65rem" }}>
                    <div className="totals-row">
                      <span>(+) Principal recebido em cartão:</span>
                      <strong>{formatBRL(conference.card.principal_in)}</strong>
                    </div>
                    <div className="totals-row">
                      <span>(+) Taxas repassadas ao cliente:</span>
                      <strong>{formatBRL(conference.card.fee_in)}</strong>
                    </div>
                    <div className="totals-row">
                      <span>(=) Total bruto cobrado na maquininha:</span>
                      <strong>{formatBRL(conference.card.gross_in)}</strong>
                    </div>
                    <div className="totals-row">
                      <span>(−) Estornos/devoluções efetivos em cartão:</span>
                      <strong>{formatBRL(conference.card.gross_out)}</strong>
                    </div>
                    <div
                      className="totals-row"
                      style={{
                        borderTop: "1px solid var(--border)",
                        marginTop: "0.35rem",
                        paddingTop: "0.35rem",
                        fontSize: "1rem",
                      }}
                    >
                      <span><strong>Total líquido de transações (bruto cobrado):</strong></span>
                      <strong style={{ color: "var(--primary-dark)" }}>
                        {formatBRL(conference.card.expected_amount)}
                      </strong>
                    </div>
                  </div>
                  <div
                    className="muted"
                    style={{
                      fontSize: "0.76rem",
                      background: "#f6f3fb",
                      padding: "0.45rem 0.6rem",
                      borderRadius: "8px",
                      marginBottom: "0.6rem",
                    }}
                  >
                    {conference.card.settlement_note}
                  </div>
                  <div className="field" style={{ marginBottom: "0.4rem" }}>
                    <label htmlFor="conf-counted-card">
                      Valor bruto de transações conferido no relatório da maquininha (R$)
                    </label>
                    <input
                      id="conf-counted-card"
                      className="input"
                      inputMode="decimal"
                      placeholder="0,00"
                      value={countedCardInput}
                      onChange={(e) => {
                        setCountedCardInput(e.target.value);
                        setConfirmCloseStep(false);
                      }}
                    />
                  </div>
                  <div style={{ fontSize: "0.86rem", display: "flex", justifyContent: "space-between" }}>
                    <span className="muted">Diferença (Conferido − Registrado):</span>
                    <strong>
                      {liveDiffs.cardDiffCents === null ? "—" : formatBRL(liveDiffs.cardDiffCents)}
                    </strong>
                  </div>
                </div>
              </div>

              {/* Justificativa de Divergência e Observações */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
                  gap: "0.85rem",
                  marginBottom: "1rem",
                }}
              >
                <div className="field" style={{ marginBottom: 0 }}>
                  <label htmlFor="conf-justification">
                    Justificativa de divergência{" "}
                    {liveDiffs.hasDiscrepancy ? (
                      <span className="badge badge-danger">Obrigatória (há diferença)</span>
                    ) : (
                      <span className="muted">(opcional quando conferência exata)</span>
                    )}
                  </label>
                  <input
                    id="conf-justification"
                    className="input"
                    placeholder="Explique o motivo da sobra ou falta identificada na conferência"
                    value={justificationInput}
                    onChange={(e) => {
                      setJustificationInput(e.target.value);
                      setConfirmCloseStep(false);
                    }}
                    required={liveDiffs.hasDiscrepancy}
                  />
                </div>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label htmlFor="conf-closing-notes">Observações gerais do fechamento (opcional)</label>
                  <input
                    id="conf-closing-notes"
                    className="input"
                    placeholder="Observações do operador ao encerrar o turno"
                    value={closingNotesInput}
                    onChange={(e) => setClosingNotesInput(e.target.value)}
                    maxLength={255}
                  />
                </div>
              </div>

              {/* Erros de validação da prévia */}
              {previewReport && previewReport.validation_errors && previewReport.validation_errors.length > 0 && (
                <div className="alert alert-error" style={{ marginBottom: "1rem" }}>
                  <strong>Não é possível encerrar o turno ainda:</strong>
                  <ul style={{ margin: "0.35rem 0 0 1.2rem", padding: 0 }}>
                    {previewReport.validation_errors.map((err, i) => (
                      <li key={i}>{err}</li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Prévia de confirmação */}
              {previewReport && previewReport.ready_to_close && (
                <div className="alert alert-success" style={{ marginBottom: "1rem" }}>
                  <strong>Prévia do fechamento validada!</strong> Todos os requisitos e conciliações foram verificados.
                  Confira os saldos acima e clique em <strong>Confirmar Encerramento Definitivo do Turno</strong>.
                </div>
              )}

              <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap", justifyContent: "flex-end" }}>
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={handlePreviewClosing}
                  disabled={previewBusy || closingBusy}
                >
                  {previewBusy ? "Validando Prévia..." : "Gerar Prévia do Fechamento"}
                </button>
                {!confirmCloseStep ? (
                  <button
                    type="button"
                    className="btn btn-danger"
                    onClick={() => {
                      if (!pendencies.can_close) {
                        setFeedback({
                          type: "error",
                          text: "Resolva todas as comandas abertas, créditos pendentes e devoluções pendentes antes de fechar o caixa.",
                        });
                        return;
                      }
                      if (liveDiffs.hasDiscrepancy && justificationInput.trim().length < 3) {
                        setFeedback({
                          type: "error",
                          text: "Informe uma justificativa (mínimo 3 caracteres) para a diferença apontada na conferência.",
                        });
                        return;
                      }
                      setConfirmCloseStep(true);
                    }}
                    disabled={!pendencies.can_close || closingBusy}
                  >
                    Encerrar Turno
                  </button>
                ) : (
                  <button
                    type="button"
                    className="btn btn-danger"
                    onClick={handleConfirmCloseShift}
                    disabled={closingBusy || !pendencies.can_close}
                  >
                    {closingBusy
                      ? "Encerrando Turno..."
                      : "Confirmar Encerramento Definitivo do Turno"}
                  </button>
                )}
              </div>
            </div>
          )}

          {/* Registro de Sangria e Suprimento + Histórico de Movimentos da Gaveta */}
          <div className="pos-layout" style={{ marginBottom: "1.5rem" }}>
            <div className="card">
              <h3>Registrar Sangria ou Suprimento (Gaveta)</h3>
              <p className="muted" style={{ fontSize: "0.86rem", marginTop: 0 }}>
                <strong>Sangria:</strong> retirada de dinheiro da gaveta (ex.: depósito em cofre).{" "}
                <strong>Suprimento:</strong> adição de dinheiro para troco. Não altera vendas nem depósitos de clientes.
              </p>
              <form onSubmit={handleDrawerMovement}>
                <div style={{ display: "flex", gap: "0.5rem", marginBottom: "0.85rem" }}>
                  <button
                    type="button"
                    className={`btn btn-sm ${drawerKind === "withdrawal" ? "btn-danger" : "btn-secondary"}`}
                    style={{ flex: 1 }}
                    onClick={() => setDrawerKind("withdrawal")}
                  >
                    Sangria (Retirada −)
                  </button>
                  <button
                    type="button"
                    className={`btn btn-sm ${drawerKind === "supply" ? "btn-success" : "btn-secondary"}`}
                    style={{ flex: 1 }}
                    onClick={() => setDrawerKind("supply")}
                  >
                    Suprimento (Entrada +)
                  </button>
                </div>

                <div className="field">
                  <label htmlFor="drawer-amount">
                    Valor d{drawerKind === "withdrawal" ? "a Sangria" : "o Suprimento"} em Dinheiro (R$)
                  </label>
                  <input
                    id="drawer-amount"
                    className="input"
                    inputMode="decimal"
                    placeholder="Ex.: 100,00"
                    value={drawerAmountInput}
                    onChange={(e) => setDrawerAmountInput(e.target.value)}
                    required
                  />
                </div>

                <div className="field">
                  <label htmlFor="drawer-reason">Motivo obrigatório</label>
                  <input
                    id="drawer-reason"
                    className="input"
                    placeholder={
                      drawerKind === "withdrawal"
                        ? "Ex.: Retirada de segurança para o cofre"
                        : "Ex.: Adição de cédulas de R$ 2 e R$ 5 para troco"
                    }
                    value={drawerReasonInput}
                    onChange={(e) => setDrawerReasonInput(e.target.value)}
                    maxLength={255}
                    required
                  />
                </div>

                <button
                  type="submit"
                  className={`btn ${drawerKind === "withdrawal" ? "btn-danger" : "btn-success"}`}
                  style={{ width: "100%" }}
                  disabled={drawerBusy}
                >
                  {drawerBusy
                    ? "Registrando..."
                    : drawerKind === "withdrawal"
                      ? "Confirmar Sangria"
                      : "Confirmar Suprimento"}
                </button>
              </form>
            </div>

            <div className="card">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.5rem" }}>
                <h3 style={{ margin: 0 }}>Movimentações de Gaveta no Turno</h3>
                <span className="muted" style={{ fontSize: "0.85rem" }}>
                  Supr.: +{formatBRL(conference.cash.supplies)} · Sangr.: −{formatBRL(conference.cash.withdrawals)}
                </span>
              </div>
              {drawerMovements.length === 0 ? (
                <p className="muted">Nenhuma sangria ou suprimento registrado neste turno.</p>
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Horário</th>
                        <th>Tipo</th>
                        <th>Valor</th>
                        <th>Motivo</th>
                        <th>Operador</th>
                      </tr>
                    </thead>
                    <tbody>
                      {drawerMovements.map((m) => (
                        <tr key={m.id}>
                          <td>{formatDateTime(m.created_at)}</td>
                          <td>
                            {m.kind === "cash_supply" ? (
                              <span className="badge badge-success">Suprimento (+)</span>
                            ) : (
                              <span className="badge badge-danger">Sangria (−)</span>
                            )}
                          </td>
                          <td>
                            <strong>
                              {m.kind === "cash_supply" ? "+" : "−"}
                              {formatBRL(m.amount)}
                            </strong>
                          </td>
                          <td>{m.reason || m.note || "—"}</td>
                          <td>{m.operator_name || `#${m.operator_id}`}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* 3. Visualização do Relatório de Fechamento Selecionado (Imprimível) */}
      {selectedReport && (
        <div
          className="card print-report"
          style={{
            marginBottom: "1.5rem",
            border: "2px solid var(--primary-dark)",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "flex-start",
              flexWrap: "wrap",
              gap: "1rem",
              borderBottom: "1px solid var(--border)",
              paddingBottom: "0.85rem",
              marginBottom: "1rem",
            }}
          >
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
                <h2 style={{ margin: 0 }}>
                  Relatório de Fechamento — Turno #{selectedReport.shift.id}
                </h2>
                <span
                  className={`badge ${
                    selectedReport.shift.status === "closed" ? "badge-admin" : "badge-warning"
                  }`}
                >
                  {selectedReport.shift.status === "closed" ? "ENCERRADO" : "ABERTO"}
                </span>
              </div>
              <div className="muted" style={{ fontSize: "0.88rem", marginTop: "0.3rem" }}>
                Abertura: <strong>{formatDateTime(selectedReport.shift.opened_at)}</strong> por{" "}
                <strong>
                  {selectedReport.shift.opened_by_name || `#${selectedReport.shift.opened_by}`}
                </strong>{" "}
                · Encerramento: <strong>{formatDateTime(selectedReport.shift.closed_at)}</strong> por{" "}
                <strong>
                  {selectedReport.shift.closed_by_name ||
                    (selectedReport.shift.closed_by ? `#${selectedReport.shift.closed_by}` : "—")}
                </strong>
              </div>
              {selectedReport.shift.notes && (
                <div style={{ fontSize: "0.88rem", marginTop: "0.2rem" }}>
                  <strong>Observações do turno:</strong> {selectedReport.shift.notes}
                </div>
              )}
            </div>
            <div className="no-print" style={{ display: "flex", gap: "0.5rem" }}>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => window.print()}
              >
                Imprimir Relatório
              </button>
              <button
                type="button"
                className="btn btn-sm btn-secondary"
                onClick={() => setSelectedReport(null)}
              >
                Fechar Visualização
              </button>
            </div>
          </div>

          {/* Resumo Operacional do Relatório */}
          <div
            className="grid"
            style={{
              gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
              marginTop: 0,
              marginBottom: "1rem",
            }}
          >
            <div className="totals-box" style={{ margin: 0 }}>
              <div className="stat-label">Fundo Inicial (Troco)</div>
              <div className="stat-value">{formatBRL(selectedReport.shift.opening_cash)}</div>
            </div>
            <div className="totals-box" style={{ margin: 0 }}>
              <div className="stat-label">Vendas de Produtos</div>
              <div className="stat-value">
                {formatBRL(selectedReport.summary.active_sales_subtotal)}
              </div>
              <div className="muted" style={{ fontSize: "0.78rem" }}>
                Rápidas: {formatBRL(selectedReport.summary.quick_sales_subtotal ?? "0.00")} · Comandas:{" "}
                {formatBRL(selectedReport.summary.tabs_consumed_subtotal ?? "0.00")}
              </div>
            </div>
            <div className="totals-box" style={{ margin: 0 }}>
              <div className="stat-label">Depósitos e Créditos</div>
              <div className="stat-value">
                Dep.: {formatBRL(selectedReport.summary.tabs_credit_deposited_total ?? "0.00")}
              </div>
              <div className="muted" style={{ fontSize: "0.78rem" }}>
                Utilizado: {formatBRL(selectedReport.summary.tabs_credit_applied_total ?? "0.00")} · Devolvido:{" "}
                {formatBRL(selectedReport.summary.tabs_credit_refunded_total ?? "0.00")}
              </div>
            </div>
            <div className="totals-box" style={{ margin: 0 }}>
              <div className="stat-label">Taxas de Cartão Repassadas</div>
              <div className="stat-value">
                {formatBRL(selectedReport.summary.active_card_fees)}
              </div>
              <div className="muted" style={{ fontSize: "0.78rem" }}>
                Devoluções/Estornos: {formatBRL(selectedReport.summary.completed_customer_refunds_total)}
              </div>
            </div>
            <div className="totals-box" style={{ margin: 0 }}>
              <div className="stat-label">Suprimentos / Sangrias</div>
              <div className="stat-value">
                +{formatBRL(selectedReport.conference.cash.supplies)} / −
                {formatBRL(selectedReport.conference.cash.withdrawals)}
              </div>
            </div>
          </div>

          {/* Tabela de Conferência por Método no Relatório */}
          <h3 style={{ marginBottom: "0.5rem" }}>Conferência por Método de Pagamento</h3>
          <div className="table-wrap" style={{ marginBottom: "1rem" }}>
            <table className="table">
              <thead>
                <tr>
                  <th>Método</th>
                  <th>Detalhamento Registrado</th>
                  <th>Valor Esperado</th>
                  <th>Valor Conferido</th>
                  <th>Diferença</th>
                  <th>Resultado</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td><strong>Dinheiro</strong></td>
                  <td style={{ fontSize: "0.84rem" }}>
                    Fundo {formatBRL(selectedReport.conference.cash.opening_cash)} + Entradas{" "}
                    {formatBRL(selectedReport.conference.cash.inflows)} − Saídas{" "}
                    {formatBRL(selectedReport.conference.cash.outflows)} + Supr.{" "}
                    {formatBRL(selectedReport.conference.cash.supplies)} − Sangr.{" "}
                    {formatBRL(selectedReport.conference.cash.withdrawals)}
                  </td>
                  <td><strong>{formatBRL(selectedReport.conference.cash.expected_amount)}</strong></td>
                  <td>
                    {selectedReport.conference.cash.counted_amount !== null
                      ? formatBRL(selectedReport.conference.cash.counted_amount)
                      : "—"}
                  </td>
                  <td>
                    <strong>
                      {selectedReport.conference.cash.difference !== null
                        ? formatBRL(selectedReport.conference.cash.difference)
                        : "—"}
                    </strong>
                  </td>
                  <td>
                    {diffBadge(
                      selectedReport.conference.cash.difference !== null
                        ? parseToCents(selectedReport.conference.cash.difference.replace("-", "")) !== null
                          ? Math.round(Number(selectedReport.conference.cash.difference) * 100)
                          : 0
                        : null,
                      selectedReport.conference.cash.status,
                    )}
                  </td>
                </tr>
                <tr>
                  <td><strong>Pix</strong></td>
                  <td style={{ fontSize: "0.84rem" }}>
                    Entradas {formatBRL(selectedReport.conference.pix.inflows)} − Devoluções{" "}
                    {formatBRL(selectedReport.conference.pix.outflows)}
                  </td>
                  <td><strong>{formatBRL(selectedReport.conference.pix.expected_amount)}</strong></td>
                  <td>
                    {selectedReport.conference.pix.counted_amount !== null
                      ? formatBRL(selectedReport.conference.pix.counted_amount)
                      : "—"}
                  </td>
                  <td>
                    <strong>
                      {selectedReport.conference.pix.difference !== null
                        ? formatBRL(selectedReport.conference.pix.difference)
                        : "—"}
                    </strong>
                  </td>
                  <td>
                    {diffBadge(
                      selectedReport.conference.pix.difference !== null
                        ? Math.round(Number(selectedReport.conference.pix.difference) * 100)
                        : null,
                      selectedReport.conference.pix.status,
                    )}
                  </td>
                </tr>
                <tr>
                  <td><strong>Cartão (Bruto Maquininha)</strong></td>
                  <td style={{ fontSize: "0.84rem" }}>
                    Principal {formatBRL(selectedReport.conference.card.principal_in)} + Taxas{" "}
                    {formatBRL(selectedReport.conference.card.fee_in)} − Estornos{" "}
                    {formatBRL(selectedReport.conference.card.gross_out)}
                  </td>
                  <td><strong>{formatBRL(selectedReport.conference.card.expected_amount)}</strong></td>
                  <td>
                    {selectedReport.conference.card.counted_amount !== null
                      ? formatBRL(selectedReport.conference.card.counted_amount)
                      : "—"}
                  </td>
                  <td>
                    <strong>
                      {selectedReport.conference.card.difference !== null
                        ? formatBRL(selectedReport.conference.card.difference)
                        : "—"}
                    </strong>
                  </td>
                  <td>
                    {diffBadge(
                      selectedReport.conference.card.difference !== null
                        ? Math.round(Number(selectedReport.conference.card.difference) * 100)
                        : null,
                      selectedReport.conference.card.status,
                    )}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          {selectedReport.conference.discrepancy_justification && (
            <div className="alert alert-warning" style={{ marginBottom: "0.85rem" }}>
              <strong>Justificativa registrada para a divergência:</strong>{" "}
              {selectedReport.conference.discrepancy_justification}
            </div>
          )}

          {selectedReport.drawer_movements.length > 0 && (
            <div>
              <h3 style={{ marginBottom: "0.4rem" }}>Sangrias e Suprimentos do Turno</h3>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Horário</th>
                      <th>Operação</th>
                      <th>Valor</th>
                      <th>Motivo</th>
                      <th>Operador</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selectedReport.drawer_movements.map((m) => (
                      <tr key={m.id}>
                        <td>{formatDateTime(m.created_at)}</td>
                        <td>{m.kind === "cash_supply" ? "Suprimento (+)" : "Sangria (−)"}</td>
                        <td>
                          {m.kind === "cash_supply" ? "+" : "−"}
                          {formatBRL(m.amount)}
                        </td>
                        <td>{m.reason || m.note || "—"}</td>
                        <td>{m.operator_name || `#${m.operator_id}`}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* 4. Histórico de Turnos Encerrados */}
      <div className="card no-print">
        <h3>Histórico de Turnos Encerrados</h3>
        {closedShifts.length === 0 ? (
          <p className="muted">Nenhum turno encerrado registrado até o momento.</p>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Turno</th>
                  <th>Abertura</th>
                  <th>Encerramento</th>
                  <th>Operador</th>
                  <th>Fundo Inicial</th>
                  <th>Dinheiro Esperado</th>
                  <th>Dinheiro Contado</th>
                  <th>Diferença Dinheiro</th>
                  <th>Justificativa</th>
                  <th>Ações</th>
                </tr>
              </thead>
              <tbody>
                {closedShifts.map((s) => {
                  const diffCents =
                    s.cash_difference !== null
                      ? Math.round(Number(s.cash_difference) * 100)
                      : null;
                  return (
                    <tr key={s.id}>
                      <td>
                        <strong>#{s.id}</strong>
                      </td>
                      <td>{formatDateTime(s.opened_at)}</td>
                      <td>{formatDateTime(s.closed_at)}</td>
                      <td>{s.closed_by_name || s.opened_by_name || `#${s.opened_by}`}</td>
                      <td>{formatBRL(s.opening_cash)}</td>
                      <td>{s.expected_cash !== null ? formatBRL(s.expected_cash) : "—"}</td>
                      <td>{s.counted_cash !== null ? formatBRL(s.counted_cash) : "—"}</td>
                      <td>{diffBadge(diffCents)}</td>
                      <td style={{ maxWidth: "240px", fontSize: "0.84rem" }}>
                        {s.discrepancy_justification || "—"}
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn btn-sm btn-secondary"
                          onClick={() => handleViewHistoricalReport(s.id)}
                          disabled={reportLoadingId === s.id}
                        >
                          {reportLoadingId === s.id ? "Carregando..." : "Ver Relatório"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
