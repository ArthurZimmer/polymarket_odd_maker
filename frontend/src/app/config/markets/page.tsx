"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  apiFetch,
  ApiError,
  FilterRef,
  FiltersResponse,
  setToken,
} from "@/lib/api";
import { Nav } from "@/components/Nav";

function keyOf(ref: { level: string; identifier: string }) {
  return `${ref.level}:${ref.identifier}`;
}

function formatAge(seconds: number | null): string {
  if (seconds === null) return "ainda não carregada";
  if (seconds < 60) return `há ${Math.floor(seconds)}s`;
  if (seconds < 3600) return `há ${Math.floor(seconds / 60)} min`;
  if (seconds < 86400) return `há ${Math.floor(seconds / 3600)} h`;
  return `há ${Math.floor(seconds / 86400)} dias`;
}

function formatStart(iso: string | null): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
  } catch {
    return iso;
  }
}

export default function MarketsPage() {
  const router = useRouter();
  const [data, setData] = useState<FiltersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Local selection state, keyed for fast lookup; meta map keeps display_name + level for serialization.
  const [selectedMeta, setSelectedMeta] = useState<Map<string, FilterRef>>(new Map());
  const [savedKeys, setSavedKeys] = useState<Set<string>>(new Set());

  const [expandedSports, setExpandedSports] = useState<Set<string>>(new Set());
  const [expandedLeagues, setExpandedLeagues] = useState<Set<string>>(new Set());

  const selectedKeys = useMemo(
    () => new Set(selectedMeta.keys()),
    [selectedMeta],
  );

  const dirty = useMemo(() => {
    if (savedKeys.size !== selectedKeys.size) return true;
    for (const k of selectedKeys) if (!savedKeys.has(k)) return true;
    return false;
  }, [savedKeys, selectedKeys]);

  function adoptResponse(d: FiltersResponse) {
    setData(d);
    const meta = new Map<string, FilterRef>();
    for (const f of d.selected) meta.set(keyOf(f), f);
    setSelectedMeta(meta);
    setSavedKeys(new Set(meta.keys()));
  }

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const d = await apiFetch<FiltersResponse>("/api/filters");
      adoptResponse(d);
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

  function toggleSelect(ref: FilterRef) {
    const k = keyOf(ref);
    setSelectedMeta((prev) => {
      const next = new Map(prev);
      if (next.has(k)) next.delete(k);
      else next.set(k, ref);
      return next;
    });
  }

  function toggleSport(id: string) {
    setExpandedSports((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleLeague(key: string) {
    setExpandedLeagues((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  async function refreshTree() {
    setRefreshing(true);
    setError(null);
    try {
      const d = await apiFetch<FiltersResponse>("/api/filters/refresh-tree", {
        method: "POST",
      });
      adoptResponse(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRefreshing(false);
    }
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const selected = Array.from(selectedMeta.values());
      const d = await apiFetch<FiltersResponse>("/api/filters", {
        method: "PUT",
        body: JSON.stringify({ selected }),
      });
      adoptResponse(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <>
        <Nav subtitle="Filtros" />
        <main className="flex flex-1 items-center justify-center">
          <p className="text-zinc-500">Carregando…</p>
        </main>
      </>
    );
  }

  const tree = data?.tree;
  const isEmpty = !tree || tree.sports.length === 0;

  return (
    <>
      <Nav subtitle="Filtros de mercado" />
      <main className="flex flex-1 flex-col bg-zinc-50 dark:bg-zinc-950">
        <div className="mx-auto w-full max-w-4xl px-4 py-6">
          {/* status bar */}
          <section className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
            <div className="text-sm">
              <p className="text-zinc-900 dark:text-zinc-100">
                Árvore com <strong>{data?.tree_event_count ?? 0}</strong> eventos
                · <span className="text-zinc-500">{formatAge(data?.tree_age_seconds ?? null)}</span>
              </p>
              <p className="mt-1 text-zinc-500">
                {selectedKeys.size} filtro{selectedKeys.size === 1 ? "" : "s"}
                {dirty && " · ⚠ alterações não salvas"}
              </p>
            </div>
            <div className="flex gap-2">
              <button
                onClick={refreshTree}
                disabled={refreshing}
                className="rounded-md border border-zinc-300 px-3 py-1 text-sm text-zinc-700 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
              >
                {refreshing ? "Atualizando…" : "Recarregar da Polymarket"}
              </button>
              <button
                onClick={save}
                disabled={saving || !dirty}
                className="rounded-md bg-zinc-900 px-3 py-1 text-sm font-medium text-white transition hover:bg-zinc-800 disabled:opacity-50 dark:bg-zinc-50 dark:text-zinc-900 dark:hover:bg-zinc-200"
              >
                {saving ? "Salvando…" : "Salvar seleção"}
              </button>
            </div>
          </section>

          {error && (
            <p className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
              {error}
            </p>
          )}

          {isEmpty ? (
            <div className="rounded-xl border border-dashed border-zinc-300 bg-white p-8 text-center text-sm text-zinc-500 dark:border-zinc-700 dark:bg-zinc-900">
              Nenhuma árvore carregada ainda. Clique em
              <strong className="mx-1">Recarregar da Polymarket</strong>
              para descobrir os mercados esportivos disponíveis.
            </div>
          ) : (
            <ul className="space-y-2">
              {tree.sports.map((sport) => {
                const sportKey = keyOf({ level: "sport", identifier: sport.id });
                const expanded = expandedSports.has(sport.id);
                return (
                  <li
                    key={sport.id}
                    className="rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900"
                  >
                    <div className="flex items-center gap-2 px-3 py-2">
                      <button
                        type="button"
                        onClick={() => toggleSport(sport.id)}
                        className="w-6 text-zinc-500"
                        aria-label={expanded ? "Recolher" : "Expandir"}
                      >
                        {expanded ? "▾" : "▸"}
                      </button>
                      <input
                        type="checkbox"
                        checked={selectedKeys.has(sportKey)}
                        onChange={() =>
                          toggleSelect({
                            level: "sport",
                            identifier: sport.id,
                            display_name: sport.label,
                          })
                        }
                        className="h-4 w-4"
                      />
                      <span className="flex-1 font-medium text-zinc-900 dark:text-zinc-100">
                        {sport.label}
                      </span>
                      <span className="text-xs text-zinc-500">
                        {sport.event_count} evento{sport.event_count === 1 ? "" : "s"}
                      </span>
                    </div>

                    {expanded && (
                      <ul className="border-t border-zinc-100 pl-6 dark:border-zinc-800">
                        {sport.leagues.map((league) => {
                          const leagueExpKey = `${sport.id}:${league.id}`;
                          const lExpanded = expandedLeagues.has(leagueExpKey);
                          const lSelKey = keyOf({
                            level: "league",
                            identifier: league.id,
                          });
                          return (
                            <li
                              key={league.id}
                              className="border-b border-zinc-100 last:border-b-0 dark:border-zinc-800"
                            >
                              <div className="flex items-center gap-2 px-3 py-1.5">
                                <button
                                  type="button"
                                  onClick={() => toggleLeague(leagueExpKey)}
                                  className="w-6 text-zinc-500"
                                  aria-label={lExpanded ? "Recolher" : "Expandir"}
                                >
                                  {lExpanded ? "▾" : "▸"}
                                </button>
                                <input
                                  type="checkbox"
                                  checked={selectedKeys.has(lSelKey)}
                                  onChange={() =>
                                    toggleSelect({
                                      level: "league",
                                      identifier: league.id,
                                      display_name: league.label,
                                    })
                                  }
                                  className="h-4 w-4"
                                />
                                <span className="flex-1 text-sm text-zinc-800 dark:text-zinc-200">
                                  {league.label}
                                </span>
                                <span className="text-xs text-zinc-500">
                                  {league.event_count}
                                </span>
                              </div>
                              {lExpanded && (
                                <ul className="max-h-72 overflow-y-auto pl-9 pb-2">
                                  {league.events.map((ev) => {
                                    const eSelKey = keyOf({
                                      level: "event",
                                      identifier: ev.id,
                                    });
                                    return (
                                      <li
                                        key={ev.id}
                                        className="flex items-center gap-2 py-1"
                                      >
                                        <input
                                          type="checkbox"
                                          checked={selectedKeys.has(eSelKey)}
                                          onChange={() =>
                                            toggleSelect({
                                              level: "event",
                                              identifier: ev.id,
                                              display_name: ev.title,
                                            })
                                          }
                                          className="h-4 w-4"
                                        />
                                        <span
                                          className="flex-1 truncate text-sm text-zinc-700 dark:text-zinc-300"
                                          title={ev.title}
                                        >
                                          {ev.title}
                                        </span>
                                        <span className="text-xs text-zinc-500">
                                          {formatStart(ev.end_date)}
                                        </span>
                                      </li>
                                    );
                                  })}
                                </ul>
                              )}
                            </li>
                          );
                        })}
                      </ul>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </main>
    </>
  );
}
