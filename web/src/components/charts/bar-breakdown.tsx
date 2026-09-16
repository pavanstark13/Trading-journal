"use client";

import * as React from "react";

import { PNL } from "@/lib/palette";
import { cn, num } from "@/lib/utils";

export interface Bucket {
  key: string;
  trades: number;
  net_profit: string;
  win_rate: string | null;
  expectancy_r: string | null;
  low_confidence: boolean;
}

/**
 * Profit by category, as bars either side of a zero baseline.
 *
 * Colour encodes sign only -- polarity, not identity and not magnitude. Bar length
 * already carries magnitude, so tinting by size would spend the one free channel on
 * information the chart shows twice.
 *
 * A bucket built from too few trades is marked rather than dropped: "GBPJPY loses
 * money" from three trades is noise, and hiding the count is how people re-plan
 * their trading around it.
 */
export function BarBreakdown({
  buckets,
  label,
  formatKey,
  emptyMessage = "Nothing to show yet",
  className,
}: {
  buckets: Bucket[];
  label: string;
  formatKey?: (key: string) => string;
  emptyMessage?: string;
  className?: string;
}) {
  if (buckets.length === 0) {
    return <p className={cn("px-4 py-8 text-center text-sm text-fg-subtle", className)}>{emptyMessage}</p>;
  }

  const values = buckets.map((b) => Number.parseFloat(b.net_profit));
  const extent = Math.max(...values.map(Math.abs), 1);
  const hasNegative = values.some((v) => v < 0);

  return (
    <div className={cn("space-y-1", className)}>
      {buckets.map((bucket) => {
        const value = Number.parseFloat(bucket.net_profit);
        const share = (Math.abs(value) / extent) * 50;
        const positive = value >= 0;
        return (
          <div
            key={bucket.key}
            className="group grid grid-cols-[6.5rem_1fr_6rem] items-center gap-2 rounded px-1.5 py-1 hover:bg-bg-sunken/70"
            title={`${bucket.trades} trade${bucket.trades === 1 ? "" : "s"}${
              bucket.win_rate ? ` · ${bucket.win_rate}% won` : ""
            }${bucket.expectancy_r ? ` · ${bucket.expectancy_r}R expectancy` : ""}`}
          >
            <span className="truncate text-xs font-medium">
              {formatKey ? formatKey(bucket.key) : bucket.key}
            </span>

            <div className="relative h-5">
              {/* Zero baseline: the second channel carrying sign, alongside colour. */}
              <div
                className="absolute inset-y-0 w-px bg-line"
                style={{ left: hasNegative ? "50%" : "0%" }}
              />
              <div
                className="absolute top-1/2 h-3 -translate-y-1/2 rounded-[3px]"
                style={{
                  background: positive ? PNL.positive : PNL.negative,
                  left: hasNegative
                    ? positive ? "50%" : `${50 - share}%`
                    : "0%",
                  width: hasNegative ? `${share}%` : `${share * 2}%`,
                }}
              />
            </div>

            <span className="flex items-center justify-end gap-1.5">
              <span className="tabular text-xs font-medium">
                {positive ? "+" : ""}
                {num(value)}
              </span>
              {bucket.low_confidence ? (
                <span
                  className="text-2xs text-fg-subtle"
                  title="Too few trades for this to mean much"
                >
                  n={bucket.trades}
                </span>
              ) : null}
            </span>
          </div>
        );
      })}
      <p className="px-1.5 pt-1 text-2xs text-fg-subtle">
        {label} · hover a row for win rate and expectancy
      </p>
    </div>
  );
}

export const WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function formatWeekday(key: string): string {
  return WEEKDAY_NAMES[Number.parseInt(key, 10)] ?? key;
}

export function formatHour(key: string): string {
  return `${key}:00`;
}
