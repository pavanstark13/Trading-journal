"use client";

import * as React from "react";

import { SERIES } from "@/lib/palette";
import { cn, num } from "@/lib/utils";

export interface EquityPoint {
  at: string;
  equity: string;
  drawdown: string;
  net_profit: string;
  trade_key: string;
}

/**
 * Account equity after each closed trade.
 *
 * One series, so there is no legend -- the title names it. The line is 2px, the
 * grid is a hairline, and a crosshair plus tooltip is standard rather than an
 * enhancement: an SVG chart that cannot be interrogated wastes the medium.
 */
export function EquityCurve({
  points,
  currency = "",
  height = 260,
  className,
}: {
  points: EquityPoint[];
  currency?: string;
  height?: number;
  className?: string;
}) {
  const [hover, setHover] = React.useState<number | null>(null);
  const svgRef = React.useRef<SVGSVGElement>(null);
  const wrapRef = React.useRef<HTMLDivElement>(null);
  const [width, setWidth] = React.useState(900);

  // One viewBox unit per pixel. With a fixed height and a fixed viewBox width the
  // default preserveAspectRatio letterboxes the plot into part of the card.
  React.useEffect(() => {
    const element = wrapRef.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      setWidth(Math.max(320, Math.round(entry.contentRect.width)));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const padding = { top: 16, right: 16, bottom: 30, left: 60 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;

  const values = points.map((p) => Number.parseFloat(p.equity));
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 0);
  const span = max - min || 1;
  // A little headroom so the line never touches the frame.
  const lo = min - span * 0.08;
  const hi = max + span * 0.08;

  const x = (index: number) =>
    padding.left + (points.length <= 1 ? plotWidth / 2 : (index / (points.length - 1)) * plotWidth);
  const y = (value: number) =>
    padding.top + plotHeight - ((value - lo) / (hi - lo || 1)) * plotHeight;

  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(values[i])}`).join(" ");
  const area = points.length
    ? `${line} L${x(points.length - 1)},${y(lo)} L${x(0)},${y(lo)} Z`
    : "";

  const ticks = React.useMemo(() => {
    const out: number[] = [];
    for (let i = 0; i <= 4; i++) out.push(lo + ((hi - lo) / 4) * i);
    return out;
  }, [lo, hi]);

  const dateTicks = React.useMemo(() => {
    if (points.length < 2) return [];
    const count = Math.min(5, points.length);
    const step = (points.length - 1) / (count - 1);
    return Array.from({ length: count }, (_, i) => Math.round(i * step));
  }, [points]);

  function onMove(event: React.MouseEvent<SVGSVGElement>) {
    const svg = svgRef.current;
    if (!svg || points.length === 0) return;
    const rect = svg.getBoundingClientRect();
    const ratio = ((event.clientX - rect.left) / rect.width) * width;
    const index = Math.round(((ratio - padding.left) / plotWidth) * (points.length - 1));
    setHover(Math.max(0, Math.min(points.length - 1, index)));
  }

  if (points.length === 0) {
    return (
      <div
        ref={wrapRef}
        className={cn("flex items-center justify-center text-sm text-fg-subtle", className)}
        style={{ height }}
      >
        No closed trades yet
      </div>
    );
  }

  const active = hover ?? null;

  return (
    <div ref={wrapRef} className={cn("relative", className)}>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${width} ${height}`}
        className="w-full"
        style={{ height }}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label="Account equity after each closed trade"
      >
        <defs>
          <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={SERIES} stopOpacity="0.18" />
            <stop offset="100%" stopColor={SERIES} stopOpacity="0" />
          </linearGradient>
        </defs>

        {/* Recessive hairline grid; solid, never dashed. */}
        {ticks.map((value) => (
          <g key={value}>
            <line
              x1={padding.left}
              x2={width - padding.right}
              y1={y(value)}
              y2={y(value)}
              stroke="hsl(var(--line))"
              strokeWidth="1"
            />
            <text
              x={padding.left - 8}
              y={y(value) + 4}
              textAnchor="end"
              className="tabular"
              fontSize="11"
              fill="hsl(var(--fg-subtle))"
            >
              {num(value, 0)}
            </text>
          </g>
        ))}

        <path d={area} fill="url(#equityFill)" />
        <path
          d={line}
          fill="none"
          stroke={SERIES}
          strokeWidth="2"
          strokeLinejoin="round"
          strokeLinecap="round"
        />

        {dateTicks.map((index) => (
          <text
            key={`d${index}`}
            x={x(index)}
            y={height - 8}
            textAnchor={index === 0 ? "start" : index === points.length - 1 ? "end" : "middle"}
            fontSize="11"
            fill="hsl(var(--fg-subtle))"
          >
            {new Date(points[index].at).toLocaleDateString(undefined, {
              day: "2-digit",
              month: "short",
            })}
          </text>
        ))}

        {active !== null ? (
          <g>
            <line
              x1={x(active)}
              x2={x(active)}
              y1={padding.top}
              y2={padding.top + plotHeight}
              stroke="hsl(var(--fg-subtle))"
              strokeWidth="1"
            />
            {/* 2px surface ring so the marker reads on top of the line. */}
            <circle
              cx={x(active)}
              cy={y(values[active])}
              r="5"
              fill={SERIES}
              stroke="hsl(var(--bg-raised))"
              strokeWidth="2"
            />
          </g>
        ) : null}
      </svg>

      {active !== null ? (
        <div
          className="pointer-events-none absolute top-2 rounded-md border border-line bg-bg-raised px-2.5 py-1.5 text-xs shadow-lg"
          style={{
            left: `calc(${(x(active) / width) * 100}% + 8px)`,
            transform: x(active) > width * 0.7 ? "translateX(-108%)" : undefined,
          }}
        >
          <p className="tabular font-medium">
            {currency} {num(values[active])}
          </p>
          <p className="tabular text-fg-muted">
            {Number.parseFloat(points[active].net_profit) >= 0 ? "+" : ""}
            {num(points[active].net_profit)} on this trade
          </p>
          <p className="mt-0.5 text-2xs text-fg-subtle">
            {new Date(points[active].at).toLocaleDateString(undefined, {
              day: "2-digit",
              month: "short",
              year: "numeric",
            })}
          </p>
        </div>
      ) : null}
    </div>
  );
}
