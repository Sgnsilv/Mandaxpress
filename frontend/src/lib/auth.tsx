"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, apiFetch, setCsrfToken } from "./api";

export type Role = "admin" | "operator";
export interface User {
  id: number;
  name: string;
  username: string;
  role: Role;
}
interface AuthPayload {
  user: User;
  csrf_token: string;
}

type AuthState =
  | { status: "loading" }
  | { status: "authenticated"; user: User }
  | { status: "error"; message: string };

interface AuthContextValue {
  state: AuthState;
  logout: () => Promise<void>;
  retry: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    apiFetch<AuthPayload>("/auth/me")
      .then((data) => {
        if (cancelled) return;
        setCsrfToken(data.csrf_token);
        setState({ status: "authenticated", user: data.user });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          router.replace("/login");
          return;
        }
        setState({
          status: "error",
          message: err instanceof ApiError ? err.message : "Falha ao verificar a sessão.",
        });
      });
    return () => {
      cancelled = true;
    };
  }, [attempt, router]);

  const logout = useCallback(async () => {
    try {
      await apiFetch("/auth/logout", { method: "POST" });
    } catch (err) {
      // Sessão já inválida no servidor equivale a estar deslogado.
      if (!(err instanceof ApiError && err.status === 401)) throw err;
    }
    setCsrfToken(null);
    router.replace("/login");
  }, [router]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  const value = useMemo(() => ({ state, logout, retry }), [state, logout, retry]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth deve ser usado dentro de AuthProvider");
  return ctx;
}
