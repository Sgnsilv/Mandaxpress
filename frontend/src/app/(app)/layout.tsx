"use client";

import { useState } from "react";
import { Brand } from "@/components/Brand";
import { AuthProvider, useAuth } from "@/lib/auth";

function Shell({ children }: { children: React.ReactNode }) {
  const { state, logout, retry } = useAuth();
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);

  if (state.status === "loading") {
    return (
      <div className="center-screen" role="status" aria-live="polite">
        <div>
          <div className="spinner" />
          <p className="muted">Verificando sessão…</p>
        </div>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className="center-screen">
        <div className="card" style={{ maxWidth: 420 }}>
          <h1 style={{ fontSize: "1.2rem" }}>Não foi possível carregar</h1>
          <p className="alert alert-error" role="alert">{state.message}</p>
          <button className="btn" onClick={retry}>Tentar novamente</button>
        </div>
      </div>
    );
  }

  async function onLogout() {
    setLoggingOut(true);
    setLogoutError(null);
    try {
      await logout();
    } catch {
      setLogoutError("Não foi possível sair. Tente novamente.");
      setLoggingOut(false);
    }
  }

  const { user } = state;
  return (
    <>
      <header className="topbar">
        <Brand />
        <div className="topbar-user">
          <span className="user-name" id="current-user">{user.name}</span>
          <span className={`badge ${user.role === "admin" ? "badge-admin" : ""}`}>
            {user.role === "admin" ? "Administrador" : "Operador"}
          </span>
          <button id="logout-button" className="btn btn-secondary" onClick={onLogout} disabled={loggingOut}>
            {loggingOut ? "Saindo…" : "Sair"}
          </button>
        </div>
      </header>
      {logoutError && <div className="container"><div className="alert alert-error" role="alert">{logoutError}</div></div>}
      {children}
    </>
  );
}

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <Shell>{children}</Shell>
    </AuthProvider>
  );
}
