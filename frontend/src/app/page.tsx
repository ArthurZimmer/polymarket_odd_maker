"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  apiFetch,
  ApiError,
  DecisionRow,
  EngineStatus,
  getToken,
  LivePnlRow,
  OrderRow,
  PositionCloseResult,
  PositionRow,
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
  const [positions, setPositions] = useState<PositionRow[]>([]);
  const [livePnl, setLivePnl] = useState<LivePnlRow[]>([]);
  const [orders, setOrders] = useState<OrderRow[]>([]);
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
        const [e, r, p, l, o] = await Promise.all([
          apiFetch<EngineStatus>("/api/decisions/status"),
          apiFetch<DecisionRow[]>(`/api/decisions/recent?limit=${PAGE_SIZE}${action}`),
          apiFetch<PositionRow[]>("/api/positions/open"),
          apiFetch<LivePnlRow[]>("/api/positions/live-pnl"),
          apiFetch<OrderRow[]>("/api/orders/recent?limit=20"),
        ]);
        if (!alive) return;
        setEngine(e);
        setRows(r);
        setPositions(p);
        setLivePnl(l);
        setOrders(o);
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
      <Nav subtitle="Decision Feed" />
      <main className="flex-1 bg-zinc-50 px-6 py-6 dark:bg-zinc-950">
        <EngineSummary engine={engine} />
        {positions.length > 0 && <PositionsTable positions={positions} livePnl={livePnl} />}
        {orders.length > 0 && <RecentOrdersTable orders={orders} />}
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

function PositionsTable({
  positions,
  livePnl,
}: {
  positions: PositionRow[];
  livePnl: LivePnlRow[];
}) {
  const [closing, setClosing] = useState<number | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const liveById = new Map(livePnl.map((l) => [l.position_id, l]));

  async function closeOne(id: number) {
    if (!confirm("Fechar essa posição agora (SELL no melhor bid)?")) return;
    setErrMsg(null);
    setClosing(id);
    try {
      const r = await apiFetch<PositionCloseResult>(
        `/api/positions/${id}/close`,
        { method: "POST" },
      );
      if (!r.success) setErrMsg(r.message);
    } catch (e) {
      setErrMsg((e as Error).message);
    } finally {
      setClosing(null);
    }
  }

  const totalUnreal = livePnl.reduce(
    (acc, l) => acc + (l.unrealized_pnl_usd ?? 0),
    0,
  );

  return (
    <div className="mb-5 rounded border border-emerald-200 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950">
      <div className="flex items-center justify-between border-b border-emerald-200 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-emerald-700 dark:border-emerald-800 dark:text-emerald-200">
        <span>
          Posições abertas ({positions.length})
          {errMsg && <span className="ml-3 normal-case text-red-600">erro: {errMsg}</span>}
        </span>
        <span
          className={
            "normal-case font-mono " +
            (totalUnreal >= 0
              ? "text-emerald-700 dark:text-emerald-300"
              : "text-red-600 dark:text-red-400")
          }
        >
          PnL não-realizado: ${totalUnreal.toFixed(2)}
        </span>
      </div>
      <table className="w-full text-left text-xs">
        <thead className="text-emerald-700/70 dark:text-emerald-300/70">
          <tr>
            <th className="px-3 py-1">Entrou</th>
            <th className="px-3 py-1">Outcome</th>
            <th className="px-3 py-1 text-right">Size</th>
            <th className="px-3 py-1 text-right">Entry</th>
            <th className="px-3 py-1 text-right">Bid</th>
            <th className="px-3 py-1 text-right">PnL</th>
            <th className="px-3 py-1">PM event</th>
            <th className="px-3 py-1 text-right">Ação</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => {
            const live = liveById.get(p.id);
            return (
              <tr key={p.id} className="border-t border-emerald-100 dark:border-emerald-900">
                <td className="px-3 py-1 font-mono">
                  {new Date(p.entry_at).toLocaleTimeString("pt-BR")}
                </td>
                <td className="px-3 py-1">{p.outcome ?? "—"}</td>
                <td className="px-3 py-1 text-right font-mono">{p.size.toFixed(2)}</td>
                <td className="px-3 py-1 text-right font-mono">{p.entry_price.toFixed(4)}</td>
                <td className="px-3 py-1 text-right font-mono text-zinc-600 dark:text-zinc-300">
                  {live?.current_bid !== undefined && live?.current_bid !== null
                    ? live.current_bid.toFixed(4)
                    : "—"}
                </td>
                <td
                  className={
                    "px-3 py-1 text-right font-mono " +
                    (live?.unrealized_pnl_usd === undefined || live?.unrealized_pnl_usd === null
                      ? "text-zinc-500"
                      : live.unrealized_pnl_usd > 0
                        ? "text-emerald-700 dark:text-emerald-300"
                        : live.unrealized_pnl_usd < 0
                          ? "text-red-600 dark:text-red-400"
                          : "text-zinc-700 dark:text-zinc-300")
                  }
                  title={
                    live?.unrealized_pct !== undefined && live?.unrealized_pct !== null
                      ? `${live.unrealized_pct.toFixed(2)}%`
                      : undefined
                  }
                >
                  {live?.unrealized_pnl_usd !== undefined && live?.unrealized_pnl_usd !== null
                    ? `$${live.unrealized_pnl_usd.toFixed(2)}`
                    : "—"}
                </td>
                <td className="px-3 py-1 text-zinc-500">{p.polymarket_event_id}</td>
                <td className="px-3 py-1 text-right">
                  <button
                    onClick={() => closeOne(p.id)}
                    disabled={closing === p.id || p.exit_order_id !== null}
                    className="rounded border border-emerald-300 bg-white px-2 py-0.5 text-[10px] font-semibold text-emerald-700 hover:bg-emerald-100 disabled:opacity-40 dark:border-emerald-700 dark:bg-emerald-900 dark:text-emerald-200 dark:hover:bg-emerald-800"
                    title={p.exit_order_id !== null ? "Já tem ordem de saída" : "Fechar agora"}
                  >
                    {closing === p.id ? "..." : p.exit_order_id !== null ? "saindo" : "Fechar"}
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RecentOrdersTable({ orders }: { orders: OrderRow[] }) {
  return (
    <div className="mb-5 rounded border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
      <div className="border-b border-zinc-200 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-zinc-600 dark:border-zinc-800 dark:text-zinc-300">
        Ordens recentes
      </div>
      <table className="w-full text-left text-xs">
        <thead className="text-zinc-500">
          <tr>
            <th className="px-3 py-1">Hora</th>
            <th className="px-3 py-1">Status</th>
            <th className="px-3 py-1">Outcome</th>
            <th className="px-3 py-1 text-right">Price</th>
            <th className="px-3 py-1 text-right">Size</th>
            <th className="px-3 py-1 text-right">Notional</th>
            <th className="px-3 py-1">Erro</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => (
            <tr key={o.id} className="border-t border-zinc-100 dark:border-zinc-800">
              <td className="px-3 py-1 font-mono text-zinc-500">
                {new Date(o.created_at).toLocaleTimeString("pt-BR")}
              </td>
              <td className="px-3 py-1">
                <OrderStatusPill status={o.status} />
              </td>
              <td className="px-3 py-1">{o.outcome ?? "—"}</td>
              <td className="px-3 py-1 text-right font-mono">{o.price.toFixed(4)}</td>
              <td className="px-3 py-1 text-right font-mono">
                {o.size.toFixed(2)}
                {o.filled_size > 0 && o.filled_size < o.size && (
                  <span className="ml-1 text-zinc-500">/{o.filled_size.toFixed(2)}</span>
                )}
              </td>
              <td className="px-3 py-1 text-right font-mono">${o.notional_usd.toFixed(2)}</td>
              <td className="px-3 py-1 text-red-600 max-w-xs truncate">{o.last_error ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OrderStatusPill({ status }: { status: string }) {
  const cls =
    status === "FILLED"
      ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-200"
      : status === "SUBMITTED" || status === "PENDING_SUBMIT"
        ? "bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-200"
        : status === "FAILED"
          ? "bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-200"
          : "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300";
  return (
    <span className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${cls}`}>
      {status}
    </span>
  );
}
