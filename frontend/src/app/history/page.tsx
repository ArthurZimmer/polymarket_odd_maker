"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  apiDownload,
  apiFetch,
  ApiError,
  getToken,
  HistoryPositionRow,
  HistorySummary,
  PnlDailyPoint,
} from "@/lib/api";
import { Nav } from "@/components/Nav";
import { PnlChart } from "@/components/PnlChart";

const REFRESH_MS = 10000;

type StatusFilter = "ALL" | "OPEN" | "CLOSED";

export default function HistoryPage() {
  const router = useRouter();
  const [authChecked, setAuthChecked] = useState(false);
  const [summary, setSummary] = useState<HistorySummary | null>(null);
  const [points, setPoints] = useState<PnlDailyPoint[]>([]);
  const [positions, setPositions] = useState<HistoryPositionRow[]>([]);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("ALL");
  const [days, setDays] = useState<number>(30);
  const [exporting, setExporting] = useState(false);
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
        const statusParam = statusFilter === "ALL" ? "" : `?status=${statusFilter}`;
        const [s, p, rows] = await Promise.all([
          apiFetch<HistorySummary>("/api/history/summary"),
          apiFetch<PnlDailyPoint[]>(`/api/history/pnl-daily?days=${days}`),
          apiFetch<HistoryPositionRow[]>(`/api/history/positions${statusParam}&limit=200`.replace("?&", "?")),
        ]);
        if (!alive) return;
        setSummary(s);
        setPoints(p);
        setPositions(rows);
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
  }, [authChecked, days, router, statusFilter]);

  async function exportCsv() {
    setExporting(true);
    setError(null);
    try {
      const param = statusFilter === "ALL" ? "" : `?status=${statusFilter}`;
      await apiDownload(`/api/history/export.csv${param}`, "poly-scraper-history.csv");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setExporting(false);
    }
  }

  if (!authChecked) {
    return (
      <main className="flex flex-1 items-center justify-center bg-zinc-50 dark:bg-zinc-950">
        <p className="text-zinc-500">Carregando…</p>
      </main>
    );
  }

  return (
    <>
      <Nav subtitle="History & PnL" />
      <main className="flex-1 bg-zinc-50 px-6 py-6 dark:bg-zinc-950">
        {error && (
          <div className="mb-3 rounded border border-red-300 bg-red-50 p-2 text-sm text-red-700 dark:border-red-700 dark:bg-red-950 dark:text-red-300">
            Erro: {error}
          </div>
        )}

        <SummaryCards summary={summary} />

        <section className="mt-5 rounded-lg border border-zinc-200 bg-white p-5 dark:border-zinc-800 dark:bg-zinc-900">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-zinc-700 dark:text-zinc-200">
              PnL acumulado — últimos {days} dias
            </h2>
            <div className="flex gap-1">
              {[7, 30, 90, 180].map((d) => (
                <button
                  key={d}
                  onClick={() => setDays(d)}
                  className={
                    "rounded px-2 py-0.5 text-xs " +
                    (d === days
                      ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                      : "bg-zinc-100 text-zinc-700 hover:bg-zinc-200 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-700")
                  }
                >
                  {d}d
                </button>
              ))}
            </div>
          </div>
          {points.length > 0 ? (
            <PnlChart points={points} />
          ) : (
            <p className="py-10 text-center text-sm text-zinc-500">
              Nenhuma posição fechada nos últimos {days} dias.
            </p>
          )}
        </section>

        <section className="mt-5 rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
          <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold text-zinc-700 dark:text-zinc-200">
                Operações ({positions.length})
              </h2>
              {(["ALL", "OPEN", "CLOSED"] as StatusFilter[]).map((s) => (
                <button
                  key={s}
                  onClick={() => setStatusFilter(s)}
                  className={
                    "rounded px-2 py-0.5 text-xs " +
                    (s === statusFilter
                      ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                      : "bg-zinc-100 text-zinc-700 hover:bg-zinc-200 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-700")
                  }
                >
                  {s}
                </button>
              ))}
            </div>
            <button
              onClick={exportCsv}
              disabled={exporting}
              className="rounded-md bg-zinc-900 px-3 py-1.5 text-xs font-semibold text-white hover:bg-zinc-800 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
            >
              {exporting ? "Exportando…" : "Exportar CSV"}
            </button>
          </div>
          <PositionsHistoryTable rows={positions} />
        </section>
      </main>
    </>
  );
}

function SummaryCards({ summary }: { summary: HistorySummary | null }) {
  if (!summary) {
    return <p className="text-sm text-zinc-500">Carregando resumo…</p>;
  }
  const pnlColor = (v: number) =>
    v > 0
      ? "text-emerald-600 dark:text-emerald-400"
      : v < 0
        ? "text-red-600 dark:text-red-400"
        : "text-zinc-700 dark:text-zinc-300";
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      <Card label="PnL total" value={`$${summary.total_pnl_usd.toFixed(2)}`} color={pnlColor(summary.total_pnl_usd)} />
      <Card label="PnL hoje" value={`$${summary.realized_pnl_today_usd.toFixed(2)}`} color={pnlColor(summary.realized_pnl_today_usd)} />
      <Card label="PnL 7d" value={`$${summary.realized_pnl_7d_usd.toFixed(2)}`} color={pnlColor(summary.realized_pnl_7d_usd)} />
      <Card label="PnL 30d" value={`$${summary.realized_pnl_30d_usd.toFixed(2)}`} color={pnlColor(summary.realized_pnl_30d_usd)} />
      <Card
        label="Win rate"
        value={summary.win_rate_pct !== null ? `${summary.win_rate_pct.toFixed(1)}%` : "—"}
      />
      <Card label="Posições fechadas" value={String(summary.closed_positions)} />
      <Card
        label="Melhor trade"
        value={summary.best_position_pnl_usd !== null ? `$${summary.best_position_pnl_usd.toFixed(2)}` : "—"}
        color="text-emerald-600 dark:text-emerald-400"
      />
      <Card
        label="Pior trade"
        value={summary.worst_position_pnl_usd !== null ? `$${summary.worst_position_pnl_usd.toFixed(2)}` : "—"}
        color="text-red-600 dark:text-red-400"
      />
    </div>
  );
}

function Card({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div>
      <div className={`mt-1 font-mono text-lg ${color ?? "text-zinc-800 dark:text-zinc-100"}`}>
        {value}
      </div>
    </div>
  );
}

function PositionsHistoryTable({ rows }: { rows: HistoryPositionRow[] }) {
  if (rows.length === 0) {
    return <p className="px-4 py-6 text-sm text-zinc-500">Sem operações no filtro.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead className="text-zinc-500">
          <tr>
            <th className="px-3 py-2">Entrou</th>
            <th className="px-3 py-2">Saiu</th>
            <th className="px-3 py-2">Esporte/Liga</th>
            <th className="px-3 py-2">Evento</th>
            <th className="px-3 py-2">Outcome</th>
            <th className="px-3 py-2 text-right">Size</th>
            <th className="px-3 py-2 text-right">Entry</th>
            <th className="px-3 py-2 text-right">Exit</th>
            <th className="px-3 py-2 text-right">EV</th>
            <th className="px-3 py-2 text-right">PnL</th>
            <th className="px-3 py-2">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-t border-zinc-100 dark:border-zinc-800">
              <td className="px-3 py-1.5 font-mono text-zinc-500">
                {new Date(r.entry_at).toLocaleString("pt-BR", {
                  day: "2-digit",
                  month: "2-digit",
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </td>
              <td className="px-3 py-1.5 font-mono text-zinc-500">
                {r.exit_at
                  ? new Date(r.exit_at).toLocaleString("pt-BR", {
                      day: "2-digit",
                      month: "2-digit",
                      hour: "2-digit",
                      minute: "2-digit",
                    })
                  : "—"}
              </td>
              <td className="px-3 py-1.5 text-zinc-600 dark:text-zinc-400">
                {r.sport && <span>{r.sport}</span>}
                {r.league && (
                  <span className="block text-zinc-400 dark:text-zinc-500">{r.league}</span>
                )}
              </td>
              <td className="px-3 py-1.5">
                <span title={r.polymarket_event_id ?? ""}>
                  {r.pm_event_title ?? r.polymarket_event_id ?? "—"}
                </span>
              </td>
              <td className="px-3 py-1.5">
                {r.outcome ?? "—"}
                {r.outcome_side && (
                  <span className="ml-1 text-zinc-500">({r.outcome_side})</span>
                )}
              </td>
              <td className="px-3 py-1.5 text-right font-mono">{r.size.toFixed(2)}</td>
              <td className="px-3 py-1.5 text-right font-mono">{r.entry_price.toFixed(4)}</td>
              <td className="px-3 py-1.5 text-right font-mono">
                {r.exit_price !== null ? r.exit_price.toFixed(4) : "—"}
              </td>
              <td className="px-3 py-1.5 text-right font-mono text-zinc-500">
                {r.ev_entry !== null ? `${(r.ev_entry * 100).toFixed(2)}%` : "—"}
              </td>
              <td
                className={
                  "px-3 py-1.5 text-right font-mono " +
                  (r.pnl_usd === null
                    ? "text-zinc-500"
                    : r.pnl_usd > 0
                      ? "text-emerald-600 dark:text-emerald-400"
                      : r.pnl_usd < 0
                        ? "text-red-600 dark:text-red-400"
                        : "text-zinc-700 dark:text-zinc-300")
                }
              >
                {r.pnl_usd !== null ? `$${r.pnl_usd.toFixed(2)}` : "—"}
              </td>
              <td className="px-3 py-1.5">
                <span
                  className={
                    "rounded px-1.5 py-0.5 text-[10px] font-semibold " +
                    (r.status === "OPEN"
                      ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-200"
                      : "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300")
                  }
                >
                  {r.status}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
