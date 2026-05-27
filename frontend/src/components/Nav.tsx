"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import {
  apiFetch,
  ApiError,
  EngineStatus,
  setToken,
  WatcherStatus,
  ScraperStatus,
  MatcherStatus,
  BotState,
  PositionManagerStatus,
  RiskStatus,
  TradingStatus,
} from "@/lib/api";

const links: Array<{ href: string; label: string }> = [
  { href: "/history", label: "History" },
  { href: "/config/wallet", label: "Wallet" },
  { href: "/config/markets", label: "Filtros" },
  { href: "/config/risk", label: "Risk" },
];

interface EnginesSnapshot {
  decision: EngineStatus | null;
  trading: TradingStatus | null;
  position: PositionManagerStatus | null;
  risk: RiskStatus | null;
}

export function Nav({ subtitle }: { subtitle?: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const [status, setStatus] = useState<WatcherStatus | null>(null);
  const [scrapers, setScrapers] = useState<ScraperStatus[] | null>(null);
  const [matcher, setMatcher] = useState<MatcherStatus | null>(null);
  const [bot, setBot] = useState<BotState | null>(null);
  const [engines, setEngines] = useState<EnginesSnapshot>({
    decision: null,
    trading: null,
    position: null,
    risk: null,
  });

  useEffect(() => {
    let alive = true;
    async function poll() {
      try {
        const [w, s, m, b, dec, trd, pos, rsk] = await Promise.all([
          apiFetch<WatcherStatus>("/api/watcher/status"),
          apiFetch<ScraperStatus[]>("/api/scrapers/status"),
          apiFetch<MatcherStatus>("/api/matcher/status"),
          apiFetch<BotState>("/api/bot/state"),
          apiFetch<EngineStatus>("/api/decisions/status").catch(() => null),
          apiFetch<TradingStatus>("/api/bot/trading-status").catch(() => null),
          apiFetch<PositionManagerStatus>("/api/positions/manager-status").catch(() => null),
          apiFetch<RiskStatus>("/api/risk/status").catch(() => null),
        ]);
        if (!alive) return;
        setStatus(w);
        setScrapers(s);
        setMatcher(m);
        setBot(b);
        setEngines({ decision: dec, trading: trd, position: pos, risk: rsk });
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          // logged out — let other pages handle the redirect
          return;
        }
        // silently swallow other errors (status pills are best-effort)
      }
    }
    poll();
    const t = setInterval(poll, 5000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  async function onLogout() {
    try {
      await apiFetch("/api/auth/logout", { method: "POST" });
    } catch {
      /* ignore */
    }
    setToken(null);
    router.replace("/login");
  }

  return (
    <header className="flex items-center justify-between border-b border-zinc-200 bg-white px-6 py-3 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex items-center gap-6">
        <h1 className="text-lg font-semibold text-zinc-900 dark:text-zinc-50">
          poly-scraper
          {subtitle && (
            <span className="ml-2 text-sm font-normal text-zinc-500">· {subtitle}</span>
          )}
        </h1>
        <nav className="flex gap-1">
          {links.map((l) => {
            const active = pathname?.startsWith(l.href);
            return (
              <Link
                key={l.href}
                href={l.href}
                className={
                  "rounded-md px-3 py-1 text-sm transition " +
                  (active
                    ? "bg-zinc-100 font-medium text-zinc-900 dark:bg-zinc-800 dark:text-zinc-50"
                    : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800")
                }
              >
                {l.label}
              </Link>
            );
          })}
        </nav>
      </div>
      <div className="flex items-center gap-3">
        <WatcherPill status={status} />
        <ScrapersPill scrapers={scrapers} />
        <MatcherPill matcher={matcher} />
        <EnginesPill engines={engines} />
        <BotPill bot={bot} />
        <button
          onClick={onLogout}
          className="rounded-md border border-zinc-300 px-3 py-1 text-sm text-zinc-700 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
        >
          Sair
        </button>
      </div>
    </header>
  );
}

function WatcherPill({ status }: { status: WatcherStatus | null }) {
  if (!status) {
    return (
      <span className="rounded-full bg-zinc-100 px-3 py-1 text-xs text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
        WS …
      </span>
    );
  }
  const ok = status.connected;
  const dot = ok ? "bg-emerald-500" : "bg-red-500";
  const label = ok ? "WS conectado" : "WS desconectado";
  const ratePerSec = (status.updates_per_min / 60).toFixed(1);
  return (
    <span
      className="flex items-center gap-2 rounded-full bg-zinc-100 px-3 py-1 text-xs text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
      title={
        `${label} · ${status.subscribed_events} eventos / ${status.subscribed_tokens} tokens` +
        (status.subscription_truncated ? " (truncado)" : "") +
        `\nTotal de mensagens: ${status.total_messages.toLocaleString("pt-BR")}` +
        (status.last_message_at ? `\nÚltima msg: ${new Date(status.last_message_at).toLocaleTimeString("pt-BR")}` : "")
      }
    >
      <span className={`inline-block h-2 w-2 rounded-full ${dot}`} />
      <span>{status.subscribed_events}ev / {status.subscribed_tokens}tk</span>
      <span className="text-zinc-500">· {status.updates_per_min.toFixed(0)}/min ({ratePerSec}/s)</span>
    </span>
  );
}

function ScrapersPill({ scrapers }: { scrapers: ScraperStatus[] | null }) {
  if (!scrapers) {
    return (
      <span className="rounded-full bg-zinc-100 px-3 py-1 text-xs text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
        Scrapers …
      </span>
    );
  }
  if (scrapers.length === 0) {
    return (
      <span className="rounded-full bg-zinc-100 px-3 py-1 text-xs text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
        Scrapers offline
      </span>
    );
  }
  const ok = scrapers.filter((s) => s.health === "ok").length;
  const degraded = scrapers.filter((s) => s.health === "degraded").length;
  const offline = scrapers.filter((s) => s.health === "offline").length;
  const dot =
    offline > 0
      ? "bg-red-500"
      : degraded > 0
        ? "bg-amber-500"
        : "bg-emerald-500";
  const totalSnapshots = scrapers.reduce(
    (acc, s) => acc + s.total_snapshots_published,
    0,
  );
  const title = scrapers
    .map((s) => {
      const last = s.last_success_at
        ? new Date(s.last_success_at).toLocaleTimeString("pt-BR")
        : "nunca";
      const err = s.last_error ? `\n    erro: ${s.last_error}` : "";
      return (
        `${s.name} [${s.health}] · ${s.total_snapshots_published.toLocaleString("pt-BR")} snaps · ` +
        `intervalo ${s.interval_s.toFixed(0)}s · última OK ${last}${err}`
      );
    })
    .join("\n");

  return (
    <span
      className="flex items-center gap-2 rounded-full bg-zinc-100 px-3 py-1 text-xs text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
      title={title}
    >
      <span className={`inline-block h-2 w-2 rounded-full ${dot}`} />
      <span>
        Scrapers {ok}/{scrapers.length}
      </span>
      <span className="text-zinc-500">
        · {totalSnapshots.toLocaleString("pt-BR")} snaps
      </span>
    </span>
  );
}

function MatcherPill({ matcher }: { matcher: MatcherStatus | null }) {
  if (!matcher) {
    return (
      <span className="rounded-full bg-zinc-100 px-3 py-1 text-xs text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
        Matcher …
      </span>
    );
  }
  if (matcher.total_runs === 0) {
    return (
      <span className="rounded-full bg-zinc-100 px-3 py-1 text-xs text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
        Matcher iniciando…
      </span>
    );
  }
  const cov = matcher.coverage_pct;
  const dot = cov >= 80 ? "bg-emerald-500" : cov >= 50 ? "bg-amber-500" : "bg-red-500";
  const sportLines = Object.entries(matcher.coverage_by_sport)
    .filter(([, c]) => c.matchable > 0 || c.parseable > 0)
    .sort((a, b) => b[1].parseable - a[1].parseable)
    .map(([sport, c]) => {
      const pct = c.matchable > 0 ? (100 * c.matched) / c.matchable : 0;
      return `  ${sport}: ${c.matched}/${c.matchable} matchable (${pct.toFixed(0)}%) · ${c.parseable} parseable`;
    })
    .join("\n");
  const lastRun = matcher.last_run_at
    ? new Date(matcher.last_run_at).toLocaleTimeString("pt-BR")
    : "—";
  const title =
    `Matcher · ${matcher.last_matches_total.toLocaleString("pt-BR")} matches totais · ` +
    `${matcher.last_pm_events_matchable} matchable / ` +
    `${matcher.last_pm_events_parseable} parseable / ` +
    `${matcher.last_pm_events_scanned} scaneados\n` +
    `Última corrida: ${lastRun} (${(matcher.last_run_duration_ms ?? 0).toFixed(0)}ms)\n\n` +
    `Cobertura por esporte (matched/matchable):\n${sportLines}`;
  return (
    <span
      className="flex items-center gap-2 rounded-full bg-zinc-100 px-3 py-1 text-xs text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
      title={title}
    >
      <span className={`inline-block h-2 w-2 rounded-full ${dot}`} />
      <span>
        Match {matcher.last_matches_total.toLocaleString("pt-BR")}
      </span>
      <span className="text-zinc-500">· {cov.toFixed(0)}% cob</span>
    </span>
  );
}

function BotPill({ bot }: { bot: BotState | null }) {
  if (!bot) {
    return (
      <span className="rounded-full bg-zinc-100 px-3 py-1 text-xs text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
        Bot …
      </span>
    );
  }
  const paused = !!bot.last_pause_reason && !bot.is_running;
  const live = bot.is_running && bot.vault_unlocked;
  const dot = paused
    ? "bg-red-500"
    : live
      ? "bg-emerald-500"
      : bot.is_running
        ? "bg-amber-500"
        : "bg-zinc-400";
  const label = paused
    ? "PAUSED"
    : live
      ? "LIVE"
      : bot.is_running
        ? "ON · vault locked"
        : "OFF";
  const baseTitle =
    `Bot ${label}\n` +
    `Stake: $${bot.master_stake_usd.toFixed(2)} · EV≥${(bot.ev_threshold * 100).toFixed(1)}%\n` +
    `Window: ${bot.min_time_to_game_minutes}–${bot.max_time_to_game_minutes}min · ` +
    `max ${bot.max_concurrent_positions} concorrentes\n` +
    `Drawdown lim: $${bot.max_daily_drawdown_usd.toFixed(0)} · ` +
    `Exposição máx: $${bot.max_total_exposure_usd.toFixed(0)}`;
  const title = paused
    ? `${baseTitle}\n\n⚠ Pausado: ${bot.last_pause_reason}`
    : baseTitle;
  const cls = paused
    ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-200"
    : live
      ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-200"
      : "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300";
  return (
    <span
      className={"flex items-center gap-2 rounded-full px-3 py-1 text-xs " + cls}
      title={title}
    >
      <span className={`inline-block h-2 w-2 rounded-full ${dot}`} />
      <span className="font-medium">{label}</span>
      <span className="text-zinc-500">· ${bot.master_stake_usd.toFixed(0)}</span>
    </span>
  );
}

function EnginesPill({ engines }: { engines: EnginesSnapshot }) {
  const { decision, trading, position, risk } = engines;
  const STALE_MS = 90_000;

  function fresh(iso: string | null | undefined): boolean {
    if (!iso) return false;
    return Date.now() - new Date(iso).getTime() < STALE_MS;
  }

  const decisionOk = fresh(decision?.last_run_at);
  const tradingOk = fresh(trading?.stats?.last_run_at);
  const positionOk = fresh(position?.stats?.last_run_at);
  const riskOk = fresh(risk?.monitor?.last_run_at);
  const riskHealthy = risk?.report?.passed !== false;

  const all = [decisionOk, tradingOk, positionOk, riskOk];
  const allOk = all.every(Boolean) && riskHealthy;
  const anyOk = all.some(Boolean);

  const dot = !riskHealthy
    ? "bg-red-500"
    : allOk
      ? "bg-emerald-500"
      : anyOk
        ? "bg-amber-500"
        : "bg-zinc-400";
  const cls = !riskHealthy
    ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-200"
    : allOk
      ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-200"
      : "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300";

  function line(name: string, ok: boolean, extra?: string): string {
    return `${ok ? "✓" : "✗"} ${name}${extra ? " — " + extra : ""}`;
  }
  const tooltip =
    "Engines (último ciclo < 90s):\n" +
    line(
      "decision",
      decisionOk,
      decision?.last_run_at ? new Date(decision.last_run_at).toLocaleTimeString("pt-BR") : "sem dado",
    ) +
    "\n" +
    line(
      "trading",
      tradingOk,
      trading?.stats?.last_run_at ? new Date(trading.stats.last_run_at).toLocaleTimeString("pt-BR") : "sem dado",
    ) +
    "\n" +
    line(
      "positions",
      positionOk,
      position?.stats?.last_open_positions !== undefined
        ? `${position.stats.last_open_positions} abertas`
        : "sem dado",
    ) +
    "\n" +
    line(
      "risk",
      riskOk && riskHealthy,
      risk?.report?.violations.length
        ? risk.report.violations.map((v) => v.code).join(",")
        : "ok",
    );

  return (
    <span
      className={"flex items-center gap-2 rounded-full px-3 py-1 text-xs " + cls}
      title={tooltip}
    >
      <span className={`inline-block h-2 w-2 rounded-full ${dot}`} />
      <span className="font-medium">Engines</span>
    </span>
  );
}
