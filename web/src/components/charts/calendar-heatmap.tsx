"use client";

import * as React from "react";

import { PNL, pnlWash } from "@/lib/palette";
import { cn, num } from "@/lib/utils";

export interface DayPnl {
  date: string;
  trades: number;
  net_profit: string;
  wins: number;
  losses: number;
}

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/**
 * A month of trading, one cell per day.
 *
 * Diverging encoding: profit and loss are opposite poles with a neutral midpoint,
 * so a flat day reads as "nothing happened" rather than as a faint win. Intensity
 * is three discrete steps per arm -- past a handful of bins adjacent classes blur
 * into each other and the grid stops being readable.
 */
export function CalendarHeatmap({
  days,
  month,
  onSelectDay,
  className,
}: {
  days: DayPnl[];
  month: Date;
  onSelectDay?: (date: string) => void;
  className?: string;
}) {
  const byDate = React.useMemo(
    () => new Map(days.map((d) => [d.date, d])),
    [days],
  );

  const extent = React.useMemo(
    () => Math.max(...days.map((d) => Math.abs(Number.parseFloat(d.net_profit))), 1),
    [days],
  );

  const cells = React.useMemo(() => {
    const first = new Date(month.getFullYear(), month.getMonth(), 1);
    const last = new Date(month.getFullYear(), month.getMonth() + 1, 0);
    // Monday-first grid; most trading weeks are discussed that way.
    const lead = (first.getDay() + 6) % 7;

    const out: (string | null)[] = Array.from({ length: lead }, () => null);
    for (let day = 1; day <= last.getDate(); day++) {
      const d = new Date(month.getFullYear(), month.getMonth(), day);
      out.push(
        `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
          d.getDate(),
        ).padStart(2, "0")}`,
      );
    }
    return out;
  }, [month]);

  return (
    <div className={className}>
      <div className="mb-1.5 grid grid-cols-7 gap-1">
        {WEEKDAYS.map((name) => (
          <div key={name} className="text-center text-2xs font-medium text-fg-subtle">
            {name}
          </div>
        ))}
      </div>

      <div className="grid grid-cols-7 gap-1">
        {cells.map((date, index) => {
          if (date === null) return <div key={`pad-${index}`} />;

          const day = byDate.get(date);
          const value = day ? Number.parseFloat(day.net_profit) : 0;
          const magnitude = day ? Math.abs(value) / extent : 0;
          const dayNumber = Number.parseInt(date.slice(-2), 10);

          return (
            <button
              key={date}
              type="button"
              disabled={!day}
              onClick={() => day && onSelectDay?.(date)}
              title={
                day
                  ? `${date}: ${value >= 0 ? "+" : ""}${num(value)} · ${day.trades} trade${
                      day.trades === 1 ? "" : "s"
                    } · ${day.wins}W ${day.losses}L`
                  : date
              }
              className={cn(
                "relative aspect-square rounded-md border text-left transition-colors",
                day
                  ? "border-transparent hover:border-fg-subtle/40 cursor-pointer"
                  : "border-line/40 cursor-default",
              )}
              style={{ background: day ? pnlWash(value, magnitude) : undefined }}
            >
              <span
                className={cn(
                  "absolute left-1 top-0.5 text-2xs",
                  magnitude > 0.55 && day ? "text-white/80" : "text-fg-subtle",
                )}
              >
                {dayNumber}
              </span>
              {day ? (
                <span
                  className={cn(
                    "tabular absolute inset-x-0 bottom-1 text-center text-[10px] font-semibold leading-none",
                    magnitude > 0.55 ? "text-white" : "text-fg",
                  )}
                >
                  {value >= 0 ? "+" : ""}
                  {num(value, 0)}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>

      <Legend />
    </div>
  );
}

function Legend() {
  return (
    <div className="mt-3 flex items-center justify-end gap-3 text-2xs text-fg-subtle">
      <span className="flex items-center gap-1.5">
        <span
          className="h-3 w-3 rounded-sm"
          style={{ background: PNL.negative }}
        />
        Loss
      </span>
      <span className="flex items-center gap-1.5">
        <span className="h-3 w-3 rounded-sm border border-line" />
        No trades
      </span>
      <span className="flex items-center gap-1.5">
        <span
          className="h-3 w-3 rounded-sm"
          style={{ background: PNL.positive }}
        />
        Profit
      </span>
    </div>
  );
}
