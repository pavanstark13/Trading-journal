"use client";

import * as React from "react";

import { PNL } from "@/lib/palette";
import { cn } from "@/lib/utils";

/**
 * How trades are distributed in R.
 *
 * The shape is the point: a healthy distribution has losses clustered at -1R
 * (stops respected) and a right tail (winners allowed to run). Losses spread past
 * -1R mean stops are being moved.
 */
export function RDistribution({
  values,
  className,
}: {
  values: number[];
  className?: string;
}) {
  const bins = React.useMemo(() => {
    const edges = [-3, -2, -1.5, -1, -0.5, 0, 0.5, 1, 1.5, 2, 3];
    const counts = new Array(edges.length + 1).fill(0);
    for (const value of values) {
      let index = edges.findIndex((edge) => value < edge);
      if (index === -1) index = edges.length;
      counts[index] += 1;
    }
    return counts.map((count, index) => ({
      count,
      label:
        index === 0
          ? "< -3R"
          : index === edges.length
            ? "3R+"
            : `${edges[index - 1]}R`,
      positive: index > 5,
    }));
  }, [values]);

  const peak = Math.max(...bins.map((b) => b.count), 1);

  if (values.length === 0) {
    return (
      <p className={cn("px-4 py-8 text-center text-sm text-fg-subtle", className)}>
        No trades with a stop loss yet, so there is no R to measure.
      </p>
    );
  }

  return (
    <div className={className}>
      <div className="flex h-36 items-stretch gap-1">
        {bins.map((bin) => (
          <div
            key={bin.label}
            className="flex h-full flex-1 flex-col items-center justify-end gap-1"
          >
            <span className="tabular text-2xs leading-none text-fg-subtle">
              {bin.count || ""}
            </span>
            <div
              className="w-full rounded-t-[3px]"
              style={{
                height: `${(bin.count / peak) * 100}%`,
                minHeight: bin.count ? 3 : 0,
                background: bin.positive ? PNL.positive : PNL.negative,
              }}
              title={`${bin.count} trade${bin.count === 1 ? "" : "s"} near ${bin.label}`}
            />
          </div>
        ))}
      </div>
      <div className="mt-1.5 flex gap-1">
        {bins.map((bin, index) => (
          <span
            key={bin.label}
            className="flex-1 text-center text-[9px] text-fg-subtle"
          >
            {index % 2 === 0 ? bin.label : ""}
          </span>
        ))}
      </div>
    </div>
  );
}
