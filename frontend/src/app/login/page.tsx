"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Brand } from "@/components/Brand";
import { ApiError, apiFetch, setCsrfToken } from "@/lib/api";

interface LoginResponse {
  csrf_token: string;
}

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Se já houver sessão válida, pula o login.
  useEffect(() => {
    apiFetch("/auth/me")
      .then(() => router.replace("/"))
      .catch(() => undefined);
  }, [router]);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return; // evita envio duplicado por clique repetido
    setError(null);
    setSubmitting(true);
    try {
      const data = await apiFetch<LoginResponse>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      setCsrfToken(data.csrf_token);
      setPassword("");
      router.replace("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Não foi possível entrar. Tente novamente.");
      setSubmitting(false);
    }
  }

  return (
    <main className="login-wrap">
      <form className="card login-card" onSubmit={onSubmit} noValidate>
        <Brand size={40} />
        <h1 style={{ fontSize: "1.4rem", marginTop: "1rem" }}>Entrar</h1>
        <p className="login-sub">Acesse o caixa com seu usuário e senha.</p>

        {error && (
          <div className="alert alert-error" role="alert" id="login-error">
            {error}
          </div>
        )}

        <div className="field">
          <label htmlFor="username">Usuário</label>
          <input
            id="username" name="username" className="input" autoComplete="username"
            autoCapitalize="none" autoFocus required value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="password">Senha</label>
          <input
            id="password" name="password" type="password" className="input"
            autoComplete="current-password" required value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        <button id="login-submit" className="btn" type="submit" style={{ width: "100%" }}
          disabled={submitting || !username || !password}>
          {submitting ? "Entrando…" : "Entrar"}
        </button>
      </form>
    </main>
  );
}
