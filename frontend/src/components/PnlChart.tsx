"use client";

import { useEffect, useRef } from "react";
import {
  AreaSeries,
  createChart,
  IChartApi,
  ISeriesApi,
  Time,
} from "lightweight-charts";

import type { PnlDailyPoint } from "@/lib/api";

interface Props {
  points: PnlDailyPoint[];
  height?: number;
}

export function PnlChart({ points, height = 240 }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Area"> | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: {
        background: { color: "transparent" },
        textColor: "#71717a",
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: "rgba(113,113,122,0.1)" },
        horzLines: { color: "rgba(113,113,122,0.1)" },
      },
      rightPriceScale: { borderColor: "rgba(113,113,122,0.2)" },
      timeScale: { borderColor: "rgba(113,113,122,0.2)" },
    });
    const series = chart.addSeries(AreaSeries, {
      topColor: "rgba(16, 185, 129, 0.40)",
      bottomColor: "rgba(16, 185, 129, 0.05)",
      lineColor: "rgba(16, 185, 129, 1)",
      lineWidth: 2,
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
    });
    chartRef.current = chart;
    seriesRef.current = series;
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!seriesRef.current) return;
    const data = points.map((p) => ({
      time: p.date as Time,
      value: p.cumulative_pnl_usd,
    }));
    seriesRef.current.setData(data);
    chartRef.current?.timeScale().fitContent();
  }, [points]);

  return (
    <div
      ref={containerRef}
      className="w-full"
      style={{ height: `${height}px` }}
    />
  );
}
