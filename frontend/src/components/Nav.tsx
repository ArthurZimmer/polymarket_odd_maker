"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { apiFetch, ApiError, setToken, WatcherStatus, ScraperStatus } from "@/lib/api";

const links: Array<{ href: string; label: string }> = [
  { href: "/config/wallet", label: "Wallet" },
  { href: "/config/markets", label: "Filtros" },
];

export function Nav({ subtitle }: { subtitle?: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const [status, setStatus] = useState<WatcherStatus | null>(null);
  const [scrapers, setScrapers] = useState<ScraperStatus[] | null>(null);

  useEffect(() => {
    let alive = true;
    async function poll() {
      try {
        const [w, s] = await Promise.all([
          apiFetch<WatcherStatus>("/api/watcher/status"),
          apiFetch<ScraperStatus[]>("/api/scrapers/status"),
        ]);
        if (!alive) return;
        setStatus(w);
        setScrapers(s);
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
