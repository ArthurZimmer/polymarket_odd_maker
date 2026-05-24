"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  apiFetch,
  ApiError,
  DecisionRow,
  EngineStatus,
  getToken,
} from "@/lib/api";
import { Nav } from "@/components/Nav";

const ALL_ACTIONS = [
  "BUY",
  "PASS_LOW_EV",
  "PASS_WINDOW_EARLY",
  "PASS_WINDOW_LATE",
  "PASS_LIQUIDITY",
  "PASS_NO_EXT_SNAP",
  "PASS_NO_POLY_SNAP",
  "PASS_NO_MAP",
  "PASS_DEVIG_FAILED",
  "PASS_FAIR_BOUNDS",
  "ERROR",
] as const;

const REFRESH_MS = 3000;
const PAGE_SIZE = 100;

export default function Home() {
  const router = useRouter();
  const [authChecked, setAuthChecked] = useState(false);
  const [engine, setEngine] = useState<EngineStatus | null>(null);
  const [rows, setRows] = useState<DecisionRow[]>([]);
  const [filter, setFilter] = useState<string>("ALL");
  const [error, setError] = useState<string | null>(null);

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
        const action = filter === "ALL" ? "" : `&action=${filter}`;
        const [e, r] = await Promise.all([
          apiFetch<EngineStatus>("/api/decisions/status"),
          apiFetch<DecisionRow[]>(`/api/decisions/recent?limit=${PAGE_SIZE}${action}`),
        ]);
        if (!alive) return;
        setEngine(e);
        setRows(r);
        setError(null);
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
  }, [authChecked, filter, router]);

  if (!authChecked) {
    return (
      <main className="flex flex-1 items-center justify-center bg-zinc-50 dark:bg-zinc-950">
        <p className="text-zinc-500">Carregando…</p>
      </main>
    );
  }

  return (
    <>
      <Nav subtitle="Decision Feed (dry-run)" />
      <main className="flex-1 bg-zinc-50 px-6 py-6 dark:bg-zinc-950">
        <EngineSummary engine={engine} />
        <FilterBar filter={filter} setFilter={setFilter} engine={engine} />
        {error && (
          <div className="mb-3 rounded border border-red-300 bg-red-50 p-2 text-sm text-red-700 dark:border-red-700 dark:bg-red-950 dark:text-red-300">
            Erro: {error}
          </div>
        )}
        <DecisionTable rows={rows} />
      </main>
    </>
  );
}

function EngineSummary({ engine }: { engine: EngineStatus | null }) {
  if (!engine) return null;
  return (
    <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
      <Stat label="Modo" value={engine.dry_run ? "DRY-RUN" : "LIVE"} tone={engine.dry_run ? "amber" : "emerald"} />
      <Stat label="Total decisões" value={engine.total_decisions.toLocaleString("pt-BR")} />
      <Stat label="BUYs (total)" value={engine.total_buys.toLocaleString("pt-BR")} tone="emerald" />
      <Stat label="BUYs no último ciclo" value={engine.last_buys.toLocaleString("pt-BR")} />
      <Stat
        label="Eval / ciclo"
        value={engine.last_evaluations.toLocaleString("pt-BR")}
      />
      <Stat
        label="Latência"
        value={engine.last_run_duration_ms != null ? `${engine.last_run_duration_ms.toFixed(0)}ms` : "—"}
      />
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "amber" | "emerald";
}) {
  const toneCls =
    tone === "emerald"
      ? "text-emerald-700 dark:text-emerald-300"
      : tone === "amber"
        ? "text-amber-700 dark:text-amber-300"
        : "text-zinc-900 dark:text-zinc-100";
  return (
    <div className="rounded border border-zinc-200 bg-white px-3 py-2 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div>
      <div className={`text-lg font-semibold ${toneCls}`}>{value}</div>
    </div>
  );
}

function FilterBar({
  filter,
  setFilter,
  engine,
}: {
  filter: string;
  setFilter: (v: string) => void;
  engine: EngineStatus | null;
}) {
  const counts = engine?.last_passes_by_reason ?? {};
  const allCount = engine
    ? (engine.last_buys + Object.values(counts).reduce((a, b) => a + b, 0))
    : 0;
  return (
    <div className="mb-3 flex flex-wrap gap-1.5 text-xs">
      <FilterChip
        label="Todos"
        count={allCount}
        active={filter === "ALL"}
        onClick={() => setFilter("ALL")}
      />
      <FilterChip
        label="BUY"
        count={engine?.last_buys ?? 0}
        active={filter === "BUY"}
        onClick={() => setFilter("BUY")}
        tone="emerald"
      />
      {ALL_ACTIONS.filter((a) => a !== "BUY").map((a) => (
        <FilterChip
          key={a}
          label={a.replace("PASS_", "")}
          count={counts[a] ?? 0}
          active={filter === a}
          onClick={() => setFilter(a)}
        />
      ))}
    </div>
  );
}

function FilterChip({
  label,
  count,
  active,
  onClick,
  tone,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
  tone?: "emerald";
}) {
  const base = active
    ? tone === "emerald"
      ? "bg-emerald-600 text-white"
      : "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
    : "bg-white text-zinc-700 border-zinc-200 hover:bg-zinc-100 dark:bg-zinc-900 dark:text-zinc-300 dark:border-zinc-800 dark:hover:bg-zinc-800";
  return (
    <button
      onClick={onClick}
      className={`rounded-full border px-3 py-1 transition ${base}`}
    >
      {label} <span className="opacity-70">({count})</span>
    </button>
  );
}

function DecisionTable({ rows }: { rows: DecisionRow[] }) {
  if (rows.length === 0) {
    return (
      <div className="rounded border border-dashed border-zinc-300 bg-white p-6 text-center text-sm text-zinc-500 dark:border-zinc-700 dark:bg-zinc-900">
        Sem decisões pra este filtro ainda. O engine roda a cada 10s.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto rounded border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
      <table className="w-full text-left text-xs">
        <thead className="bg-zinc-50 text-zinc-500 dark:bg-zinc-950">
          <tr>
            <th className="px-3 py-2">Hora</th>
            <th className="px-3 py-2">Ação</th>
            <th className="px-3 py-2">Evento</th>
            <th className="px-3 py-2">Lado</th>
            <th className="px-3 py-2 text-right">Fair</th>
            <th className="px-3 py-2 text-right">PM ask</th>
            <th className="px-3 py-2 text-right">EV</th>
            <th className="px-3 py-2 text-right">Depth</th>
            <th className="px-3 py-2 text-right">Kickoff</th>
            <th className="px-3 py-2">Detalhe</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-t border-zinc-100 dark:border-zinc-800">
              <td className="px-3 py-1 font-mono text-zinc-500">
                {new Date(r.captured_at).toLocaleTimeString("pt-BR")}
              </td>
              <td className="px-3 py-1">
                <ActionPill action={r.action} />
              </td>
              <td className="px-3 py-1">
                <div className="font-medium text-zinc-900 dark:text-zinc-100">
                  {r.pm_event_title ?? r.polymarket_event_id}
                </div>
                <div className="text-zinc-500">
                  {r.sport} · {r.league}
                </div>
              </td>
              <td className="px-3 py-1">
                <span className="text-zinc-700 dark:text-zinc-300">{r.pm_outcome ?? "—"}</span>
                {r.outcome_side && (
                  <span className="ml-1 text-zinc-500">({r.outcome_side})</span>
                )}
              </td>
              <td className="px-3 py-1 text-right font-mono">
                {r.fair_prob != null ? r.fair_prob.toFixed(3) : "—"}
              </td>
              <td className="px-3 py-1 text-right font-mono">
                {r.poly_best_ask != null ? r.poly_best_ask.toFixed(3) : "—"}
              </td>
              <td className="px-3 py-1 text-right font-mono">
                {r.ev != null ? (
                  <span className={r.ev > 0 ? "text-emerald-600" : "text-zinc-500"}>
                    {(r.ev * 100).toFixed(2)}%
                  </span>
                ) : (
                  "—"
                )}
              </td>
              <td className="px-3 py-1 text-right font-mono text-zinc-500">
                {r.poly_ask_depth_usd != null ? `$${r.poly_ask_depth_usd.toFixed(0)}` : "—"}
              </td>
              <td className="px-3 py-1 text-right font-mono text-zinc-500">
                {r.seconds_to_kickoff != null
                  ? `${(r.seconds_to_kickoff / 60).toFixed(0)}m`
                  : "—"}
              </td>
              <td className="px-3 py-1 text-zinc-500 max-w-md truncate">
                {r.reason ?? ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ActionPill({ action }: { action: string }) {
  const isBuy = action === "BUY";
  const cls = isBuy
    ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-200"
    : action === "ERROR"
      ? "bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-200"
      : "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300";
  return (
    <span className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${cls}`}>
      {action.replace("PASS_", "")}
    </span>
  );
}
