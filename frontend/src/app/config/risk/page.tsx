"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  apiFetch,
  ApiError,
  BotState,
  BotStatePatch,
  getToken,
  TradingStatus,
} from "@/lib/api";
import { Nav } from "@/components/Nav";

const REFRESH_MS = 4000;

export default function RiskConfigPage() {
  const router = useRouter();
  const [authChecked, setAuthChecked] = useState(false);
  const [state, setState] = useState<BotState | null>(null);
  const [trading, setTrading] = useState<TradingStatus | null>(null);
  const [draft, setDraft] = useState<BotStatePatch | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<Date | null>(null);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    setAuthChecked(true);
  }, [router]);

  useEffect(() => {
    if (!authChecked) return;
    let alive = true;
    async function poll() {
      try {
        const [s, t] = await Promise.all([
          apiFetch<BotState>("/api/bot/state"),
          apiFetch<TradingStatus>("/api/bot/trading-status"),
        ]);
        if (!alive) return;
        setState(s);
        setTrading(t);
        // First load only — don't stomp on user edits.
        setDraft((prev) => prev ?? toDraft(s));
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          router.replace("/login");
          return;
        }
        if (alive) setError((e as Error).message);
      }
    }
    poll();
    const t = setInterval(poll, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [authChecked, router]);

  const dirty =
    state !== null &&
    draft !== null &&
    JSON.stringify(toDraft(state)) !== JSON.stringify(draft);

  async function save(patch: BotStatePatch) {
    setSaving(true);
    setError(null);
    try {
      const updated = await apiFetch<BotState>("/api/bot/state", {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      setState(updated);
      setDraft(toDraft(updated));
      setSavedAt(new Date());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function toggleRunning() {
    if (!state) return;
    await save({ is_running: !state.is_running });
  }

  async function saveDraft() {
    if (!draft) return;
    await save(draft);
  }

  if (!authChecked || !state || !draft) {
    return (
      <main className="flex flex-1 items-center justify-center bg-zinc-50 dark:bg-zinc-950">
        <p className="text-zinc-500">Carregando…</p>
      </main>
    );
  }

  return (
    <>
      <Nav subtitle="Risk & Bot Control" />
      <main className="flex-1 bg-zinc-50 px-6 py-6 dark:bg-zinc-950">
        <section className="mb-6 rounded-lg border border-zinc-200 bg-white p-5 dark:border-zinc-800 dark:bg-zinc-900">
          <div className="flex items-center justify-between gap-4">
            <div>
              <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100">
                Master Switch
              </h2>
              <p className="text-sm text-zinc-500">
                Quando OFF, o bot continua avaliando e logando decisões em dry-run
                mas <strong>nunca envia ordens</strong>. Quando ON, todo{" "}
                <code>BUY</code> que passa os filtros vira ordem LIMIT GTC no
                Polymarket no melhor ask visível.
              </p>
              <p className="mt-2 text-xs text-zinc-500">
                Vault: {state.vault_unlocked ? "✓ desbloqueado" : "🔒 travado — sem trade possível"}
                {trading?.stats && (
                  <>
                    {" · "}
                    {trading.stats.total_orders_submitted} ordens enviadas (sessão)
                    {" · "}
                    {trading.stats.total_fills} fills
                  </>
                )}
              </p>
            </div>
            <button
              onClick={toggleRunning}
              disabled={saving}
              className={
                "rounded-md px-4 py-2 text-sm font-semibold transition " +
                (state.is_running
                  ? "bg-red-600 text-white hover:bg-red-700 disabled:bg-red-400"
                  : "bg-emerald-600 text-white hover:bg-emerald-700 disabled:bg-emerald-400")
              }
            >
              {state.is_running ? "Desligar bot" : "Ligar bot"}
            </button>
          </div>
        </section>

        <section className="rounded-lg border border-zinc-200 bg-white p-5 dark:border-zinc-800 dark:bg-zinc-900">
          <h2 className="mb-4 text-lg font-semibold text-zinc-900 dark:text-zinc-100">
            Parâmetros de risco
          </h2>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <NumberInput
              label="Stake por trade (USD)"
              step={0.5}
              min={1}
              max={10000}
              value={draft.master_stake_usd!}
              onChange={(v) => setDraft({ ...draft, master_stake_usd: v })}
            />
            <NumberInput
              label="EV mínimo (%)"
              step={0.1}
              min={0}
              max={100}
              value={(draft.ev_threshold ?? 0) * 100}
              onChange={(v) => setDraft({ ...draft, ev_threshold: v / 100 })}
            />
            <NumberInput
              label="Limite posições concorrentes"
              step={1}
              min={1}
              max={50}
              value={draft.max_concurrent_positions!}
              onChange={(v) => setDraft({ ...draft, max_concurrent_positions: Math.round(v) })}
            />
            <NumberInput
              label="Drawdown diário (USD)"
              step={5}
              min={1}
              max={100000}
              value={draft.max_daily_drawdown_usd!}
              onChange={(v) => setDraft({ ...draft, max_daily_drawdown_usd: v })}
            />
            <NumberInput
              label="Janela mínima até kickoff (min)"
              step={1}
              min={0}
              max={600}
              value={draft.min_time_to_game_minutes!}
              onChange={(v) => setDraft({ ...draft, min_time_to_game_minutes: Math.round(v) })}
            />
            <NumberInput
              label="Janela máxima até kickoff (min)"
              step={5}
              min={1}
              max={1440}
              value={draft.max_time_to_game_minutes!}
              onChange={(v) => setDraft({ ...draft, max_time_to_game_minutes: Math.round(v) })}
            />
            <NumberInput
              label="Depth mínimo no ask (USD)"
              step={10}
              min={0}
              max={100000}
              value={draft.min_ask_depth_usd!}
              onChange={(v) => setDraft({ ...draft, min_ask_depth_usd: v })}
            />
            <NumberInput
              label="Threshold saída (convergência %)"
              step={0.1}
              min={0}
              max={100}
              value={(draft.exit_threshold ?? 0) * 100}
              onChange={(v) => setDraft({ ...draft, exit_threshold: v / 100 })}
            />
            <NumberInput
              label="Stop-loss (% queda do entry)"
              step={1}
              min={0}
              max={100}
              value={(draft.stop_loss_pct ?? 0) * 100}
              onChange={(v) => setDraft({ ...draft, stop_loss_pct: v / 100 })}
            />
          </div>

          <div className="mt-5 flex items-center justify-between border-t border-zinc-100 pt-4 dark:border-zinc-800">
            <div className="text-xs text-zinc-500">
              {error && <span className="text-red-600">Erro: {error}</span>}
              {!error && savedAt && (
                <span>Salvo às {savedAt.toLocaleTimeString("pt-BR")}</span>
              )}
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => setDraft(toDraft(state))}
                disabled={saving || !dirty}
                className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm text-zinc-700 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
              >
                Reverter
              </button>
              <button
                onClick={saveDraft}
                disabled={saving || !dirty}
                className="rounded-md bg-zinc-900 px-4 py-1.5 text-sm font-semibold text-white hover:bg-zinc-800 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
              >
                Salvar
              </button>
            </div>
          </div>
        </section>
      </main>
    </>
  );
}

function toDraft(s: BotState): BotStatePatch {
  return {
    is_running: s.is_running,
    master_stake_usd: s.master_stake_usd,
    ev_threshold: s.ev_threshold,
    exit_threshold: s.exit_threshold,
    stop_loss_pct: s.stop_loss_pct,
    max_concurrent_positions: s.max_concurrent_positions,
    max_daily_drawdown_usd: s.max_daily_drawdown_usd,
    min_time_to_game_minutes: s.min_time_to_game_minutes,
    max_time_to_game_minutes: s.max_time_to_game_minutes,
    min_ask_depth_usd: s.min_ask_depth_usd,
  };
}

function NumberInput({
  label,
  value,
  step,
  min,
  max,
  onChange,
}: {
  label: string;
  value: number;
  step: number;
  min: number;
  max: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs uppercase tracking-wide text-zinc-500">{label}</span>
      <input
        type="number"
        value={value}
        step={step}
        min={min}
        max={max}
        onChange={(e) => onChange(parseFloat(e.target.value) || 0)}
        className="rounded border border-zinc-300 bg-white px-3 py-2 text-sm font-mono text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
      />
    </label>
  );
}
