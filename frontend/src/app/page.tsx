"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { getToken } from "@/lib/api";

export default function Home() {
  const router = useRouter();
  useEffect(() => {
    router.replace(getToken() ? "/config/wallet" : "/login");
  }, [router]);
  return (
    <main className="flex flex-1 items-center justify-center bg-zinc-50 dark:bg-zinc-950">
      <p className="text-zinc-500">Carregando…</p>
    </main>
  );
}
