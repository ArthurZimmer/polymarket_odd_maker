"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch, ApiError, setToken, WalletPayload, WalletView } from "@/lib/api";
import { Nav } from "@/components/Nav";

export default function WalletPage() {
  const router = useRouter();
  const [wallet, setWallet] = useState<WalletView | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [privateKey, setPrivateKey] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [funderAddress, setFunderAddress] = useState("");

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<WalletView>("/api/wallet");
      setWallet(data);
      setFunderAddress(data.funder_address ?? "");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setToken(null);
        router.replace("/login");
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function onSave(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!privateKey) {
      setError("Cole a Private Key.");
      return;
    }
    setSubmitting(true);
    try {
      const payload: WalletPayload = {
        private_key: privateKey,
        api_key: apiKey || null,
        api_secret: apiSecret || null,
        funder_address: funderAddress || null,
      };
      const data = await apiFetch<WalletView>("/api/wallet", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      setWallet(data);
      setPrivateKey("");
      setApiKey("");
      setApiSecret("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) {
    return (
      <>
        <Nav subtitle="Wallet" />
        <main className="flex flex-1 items-center justify-center">
          <p className="text-zinc-500">Carregando…</p>
        </main>
      </>
    );
  }

  return (
    <>
      <Nav subtitle="Wallet" />
      <main className="flex flex-1 flex-col bg-zinc-50 dark:bg-zinc-950">
        <div className="mx-auto w-full max-w-2xl px-4 py-8">
        {wallet?.has_credentials ? (
          <section className="mb-6 rounded-xl border border-zinc-200 bg-white p-5 dark:border-zinc-800 dark:bg-zinc-900">
            <h2 className="text-sm font-medium text-zinc-500">Wallet ativa</h2>
            <p className="mt-1 font-mono text-sm break-all text-zinc-900 dark:text-zinc-100">
              {wallet.address}
            </p>
            <dl className="mt-3 grid grid-cols-2 gap-3 text-sm">
              <div>
                <dt className="text-zinc-500">Saldo USDC.e (Polygon)</dt>
                <dd className="text-zinc-900 dark:text-zinc-100">
                  {wallet.usdc_balance === null
                    ? "indisponível (RPC offline)"
                    : `${wallet.usdc_balance.toFixed(2)} USDC`}
                </dd>
              </div>
              <div>
                <dt className="text-zinc-500">API Key Polymarket</dt>
                <dd className="text-zinc-900 dark:text-zinc-100">
                  {wallet.has_api_key ? "configurada" : "—"}
                </dd>
              </div>
              {wallet.funder_address && (
                <div className="col-span-2">
                  <dt className="text-zinc-500">Funder address</dt>
                  <dd className="font-mono text-xs break-all text-zinc-900 dark:text-zinc-100">
                    {wallet.funder_address}
                  </dd>
                </div>
              )}
            </dl>
          </section>
        ) : (
          <p className="mb-6 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
            Nenhuma wallet configurada ainda. Cole sua Private Key abaixo.
          </p>
        )}

        <form
          onSubmit={onSave}
          className="rounded-xl border border-zinc-200 bg-white p-5 dark:border-zinc-800 dark:bg-zinc-900"
        >
          <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-50">
            {wallet?.has_credentials ? "Atualizar credenciais" : "Configurar wallet"}
          </h2>

          <label className="mt-4 block text-sm font-medium text-zinc-700 dark:text-zinc-300">
            Private Key (Polygon EOA)
            <input
              type="password"
              value={privateKey}
              onChange={(e) => setPrivateKey(e.target.value)}
              placeholder="0x..."
              autoComplete="off"
              className="mt-1 block w-full rounded-md border border-zinc-300 bg-white px-3 py-2 font-mono text-sm text-zinc-900 outline-none focus:border-zinc-500 focus:ring-1 focus:ring-zinc-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
            />
          </label>

          <label className="mt-3 block text-sm font-medium text-zinc-700 dark:text-zinc-300">
            Polymarket API Key (opcional)
            <input
              type="text"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              autoComplete="off"
              className="mt-1 block w-full rounded-md border border-zinc-300 bg-white px-3 py-2 font-mono text-sm text-zinc-900 outline-none focus:border-zinc-500 focus:ring-1 focus:ring-zinc-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
            />
          </label>

          <label className="mt-3 block text-sm font-medium text-zinc-700 dark:text-zinc-300">
            Polymarket API Secret (opcional)
            <input
              type="password"
              value={apiSecret}
              onChange={(e) => setApiSecret(e.target.value)}
              autoComplete="off"
              className="mt-1 block w-full rounded-md border border-zinc-300 bg-white px-3 py-2 font-mono text-sm text-zinc-900 outline-none focus:border-zinc-500 focus:ring-1 focus:ring-zinc-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
            />
          </label>

          <label className="mt-3 block text-sm font-medium text-zinc-700 dark:text-zinc-300">
            Funder address (opcional — proxy wallet)
            <input
              type="text"
              value={funderAddress}
              onChange={(e) => setFunderAddress(e.target.value)}
              placeholder="0x..."
              autoComplete="off"
              className="mt-1 block w-full rounded-md border border-zinc-300 bg-white px-3 py-2 font-mono text-sm text-zinc-900 outline-none focus:border-zinc-500 focus:ring-1 focus:ring-zinc-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
            />
          </label>

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
            {submitting ? "Salvando…" : "Validar e salvar"}
          </button>
        </form>
        </div>
      </main>
    </>
  );
}
