"use client";

import { useQuery } from "@tanstack/react-query";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import {
  BarBreakdown, formatHour, formatWeekday,
} from "@/components/charts/bar-breakdown";
import { RDistribution } from "@/components/charts/r-distribution";
import {
  Badge, Card, CardBody, CardHeader, ErrorNote, Skeleton,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { Bucket, Summary, TradeRow } from "@/lib/types";
import { num } from "@/lib/utils";

const DIMENSIONS = [
  { key: "symbol", title: "By instrument", note: "Most traders make it on two and give it back on the rest." },
  { key: "hour", title: "By hour of day", note: "In your timezone. Almost everyone has a dead hour.", format: formatHour },
  { key: "weekday", title: "By day of week", note: "Mondays and Friday afternoons are usually the worst.", format: formatWeekday },
  { key: "session", title: "By session", note: "Asia, London, the overlap, New York." },
  { key: "setup", title: "By setup", note: "Needs trades tagged with a setup in your playbook." },
  { key: "direction", title: "Long vs short", note: "A large gap usually means a directional bias." },
] as const;

export default function AnalyticsPage() {
  const summary = useQuery({
    queryKey: ["summary"],
    queryFn: () => api.get<Summary>("/stats/summary"),
  });
  const trades = useQuery({
    queryKey: ["trades", "all-for-r"],
    queryFn: () => api.get<{ items: TradeRow[] }>("/trades?limit=500"),
  });

  const rValues = React.useMemo(
    () =>
      (trades.data?.items ?? [])
        .map((t) => (t.r_multiple ? Number.parseFloat(t.r_multiple) : null))
        .filter((v): v is number => v !== null),
    [trades.data],
  );

  if (summary.error) return <ErrorNote error={summary.error} />;

  return (
    <>
      <PageHeader
        title="Analytics"
        description="Where the money actually comes from, and where it goes."
        action={
          summary.data?.low_confidence ? (
            <Badge tone="warn">
              {summary.data.sample_size} trades — under {summary.data.min_meaningful_sample},
              so read these as hints
            </Badge>
          ) : null
        }
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Result distribution"
            action={<span className="text-2xs text-fg-subtle">In R</span>}
          />
          <CardBody>
            <RDistribution values={rValues} />
            <p className="mt-3 text-2xs text-fg-subtle">
              Healthy shape: losses bunched at −1R because stops were respected, and a
              tail to the right because winners were allowed to run. Losses spread past
              −1R usually mean stops were moved.
            </p>
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="The numbers" />
          <CardBody>
            {summary.isLoading || !summary.data ? (
              <Skeleton className="h-48" />
            ) : (
              <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
                <Figure label="Expectancy" value={summary.data.expectancy_r ? `${summary.data.expectancy_r}R` : "—"} />
                <Figure label="Average R" value={summary.data.avg_r ?? "—"} />
                <Figure label="Profit factor" value={summary.data.profit_factor ? num(summary.data.profit_factor, 2) : "—"} />
                <Figure
                  label="System quality"
                  value={summary.data.sqn ?? "—"}
                  hint={summary.data.sample_size < 30 ? "Needs 30+ trades" : undefined}
                />
                <Figure label="Largest win" value={num(summary.data.largest_win)} />
                <Figure label="Largest loss" value={num(summary.data.largest_loss)} />
                <Figure
                  label="Average hold"
                  value={
                    summary.data.avg_duration_seconds
                      ? humanDuration(summary.data.avg_duration_seconds)
                      : "—"
                  }
                />
                <Figure
                  label="No stop loss"
                  value={`${summary.data.trades_without_stop} trades`}
                />
              </dl>
            )}
          </CardBody>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {DIMENSIONS.map((entry) => (
          <Breakdown
            key={entry.key}
            dimension={entry.key}
            title={entry.title}
            note={entry.note}
            format={"format" in entry ? entry.format : undefined}
          />
        ))}
      </div>
    </>
  );
}

function Breakdown({
  dimension,
  title,
  note,
  format,
}: {
  dimension: string;
  title: string;
  note: string;
  format?: (key: string) => string;
}) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["breakdown", dimension],
    queryFn: () => api.get<{ buckets: Bucket[] }>(`/stats/breakdown/${dimension}`),
  });

  return (
    <Card>
      <CardHeader title={title} />
      <CardBody>
        {error ? (
          <ErrorNote error={error} />
        ) : isLoading ? (
          <Skeleton className="h-40" />
        ) : (
          <BarBreakdown
            buckets={data?.buckets ?? []}
            label={note}
            formatKey={format}
            emptyMessage="Nothing here yet"
          />
        )}
      </CardBody>
    </Card>
  );
}

function Figure({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div>
      <dt className="text-2xs uppercase tracking-wider text-fg-subtle">{label}</dt>
      <dd className="tabular mt-0.5 font-medium">{value}</dd>
      {hint ? <dd className="text-2xs text-fg-subtle">{hint}</dd> : null}
    </div>
  );
}

function humanDuration(seconds: number): string {
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}
