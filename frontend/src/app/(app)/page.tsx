"use client";

import { useAuth } from "@/lib/auth";

export default function HomePage() {
  const { state } = useAuth();
  if (state.status !== "authenticated") return null;
  const { user } = state;

  return (
    <main className="container">
      <h1>Olá, {user.name.split(" ")[0]}!</h1>
      <p className="muted">
        Fundação do Mandaxpress pronta. Venda rápida, comandas e créditos serão liberados nos próximos blocos.
      </p>
      <section className="grid" aria-label="Sessão atual">
        <div className="card">
          <div className="stat-label">Usuário</div>
          <div className="stat-value" id="home-username">{user.username}</div>
        </div>
        <div className="card">
          <div className="stat-label">Perfil</div>
          <div className="stat-value" id="home-role">{user.role === "admin" ? "Administrador" : "Operador"}</div>
        </div>
        <div className="card">
          <div className="stat-label">Sessão</div>
          <div className="stat-value">Ativa (cookie HttpOnly)</div>
        </div>
      </section>
    </main>
  );
}
