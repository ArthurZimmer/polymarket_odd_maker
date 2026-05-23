"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch, AuthState, setToken, TokenResponse } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [state, setState] = useState<AuthState | null>(null);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch<AuthState>("/api/auth/state")
      .then(setState)
      .catch((e) => setError(`Não foi possível conectar ao backend: ${e.message}`));
  }, []);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (password.length < 8) {
      setError("A senha precisa ter pelo menos 8 caracteres.");
      return;
    }
    if (state?.setup_required && password !== confirm) {
      setError("As senhas não coincidem.");
      return;
    }

    setSubmitting(true);
    try {
      const path = state?.setup_required ? "/api/auth/setup" : "/api/auth/login";
      const res = await apiFetch<TokenResponse>(path, {
        method: "POST",
        body: JSON.stringify({ password }),
      });
      setToken(res.access_token);
      router.replace("/config/wallet");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro desconhecido");
    } finally {
      setSubmitting(false);
    }
  }

  if (!state) {
    return (
      <main className="flex flex-1 items-center justify-center">
        <p className="text-zinc-500">Carregando…</p>
      </main>
    );
  }

  const setupMode = state.setup_required;
  return (
    <main className="flex flex-1 items-center justify-center bg-zinc-50 px-4 dark:bg-zinc-950">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm rounded-xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900"
      >
        <h1 className="text-xl font-semibold text-zinc-900 dark:text-zinc-50">
          {setupMode ? "Defina sua senha mestra" : "Entrar"}
        </h1>
        <p className="mt-1 text-sm text-zinc-500">
          {setupMode
            ? "Esta senha protege sua private key. Ela nunca é salva em disco — guarde com segurança."
            : "Digite a senha mestra para destravar o cofre."}
        </p>

        <label className="mt-4 block text-sm font-medium text-zinc-700 dark:text-zinc-300">
          Senha
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={setupMode ? "new-password" : "current-password"}
            required
            minLength={8}
            className="mt-1 block w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 outline-none focus:border-zinc-500 focus:ring-1 focus:ring-zinc-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
          />
        </label>

        {setupMode && (
          <label className="mt-3 block text-sm font-medium text-zinc-700 dark:text-zinc-300">
            Confirmar senha
            <input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
              minLength={8}
              className="mt-1 block w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 outline-none focus:border-zinc-500 focus:ring-1 focus:ring-zinc-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
            />
          </label>
        )}

        {error && (
          <p className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={submitting}
          className="mt-5 w-full rounded-md bg-zinc-900 px-3 py-2 text-sm font-medium text-white transition hover:bg-zinc-800 disabled:opacity-50 dark:bg-zinc-50 dark:text-zinc-900 dark:hover:bg-zinc-200"
        >
          {submitting ? "Aguarde…" : setupMode ? "Criar senha" : "Entrar"}
        </button>
      </form>
    </main>
  );
}
