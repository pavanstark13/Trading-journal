"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { PageHeader } from "@/components/app/page-header";
import { BarBreakdown, formatHour } from "@/components/charts/bar-breakdown";
import { EquityCurve } from "@/components/charts/equity-curve";
import {
  Badge, Card, CardBody, CardHeader, EmptyState, ErrorNote, Skeleton,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { AccountRow, Overview } from "@/lib/types";
import { cn, num, signed } from "@/lib/utils";

export default function DashboardPage() {
  const overview = useQuery({
    queryKey: ["overview"],
    queryFn: () => api.get<Overview>("/stats/overview"),
  });
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api.get<AccountRow[]>("/accounts"),
  });

  if (overview.error) return <ErrorNote error={overview.error} />;
  if (overview.isLoading || !overview.data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-9 w-48" />
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-24" />)}
        </div>
        <Skeleton className="h-72" />
      </div>
    );
  }

  const { summary, curves, by_symbol, by_hour, behaviour } = overview.data;
  const currency = accounts.data?.[0]?.currency ?? "";

  if (summary.trades === 0) {
    return (
      <>
        <PageHeader title="Overview" />
        <Card>
          <EmptyState
            title="No trades yet"
            hint="Connect a MetaTrader account and your whole history uploads automatically — you do not type anything in."
          />
          <CardBody className="flex justify-center pb-6 pt-0">
            <Link
              href="/accounts"
              className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-fg hover:brightness-110"
            >
              Connect an account
            </Link>
          </CardBody>
        </Card>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Overview"
        description={`${summary.trades} closed trades`}
        action={
          summary.low_confidence ? (
            <Badge tone="warn">
              Under {summary.min_meaningful_sample} trades — treat these as indicative
            </Badge>
          ) : null
        }
      />

      {/* The headline is one number, so it gets a hero tile rather than a chart. */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Headline
          label="Net profit"
          value={signed(summary.net_profit)}
          sub={currency}
          tone={Number.parseFloat(summary.net_profit) >= 0 ? "good" : "bad"}
        />
        <Headline
          label="Expectancy"
          value={summary.expectancy_r ? `${signed(summary.expectancy_r, 2)}R` : "—"}
          sub={
            summary.expectancy_r
              ? "Average result per trade risked"
              : "Needs trades with a stop loss"
          }
          tone={
            summary.expectancy_r
              ? Number.parseFloat(summary.expectancy_r) >= 0 ? "good" : "bad"
              : undefined
          }
        />
        <Headline
          label="Win rate"
          value={summary.win_rate ? `${num(summary.win_rate, 0)}%` : "—"}
          sub={`${summary.wins}W · ${summary.losses}L${
            summary.scratches ? ` · ${summary.scratches} scratch` : ""
          }`}
        />
        <Headline
          label="Max drawdown"
          value={num(summary.max_drawdown)}
          sub={
            summary.max_drawdown_pct
              ? `${num(summary.max_drawdown_pct, 1)}% off peak`
              : "No drawdown yet"
          }
          tone={Number.parseFloat(summary.max_drawdown) > 0 ? "bad" : undefined}
        />
      </div>

      <Card>
        <CardHeader
          title="Account equity"
          action={
            <span className="text-2xs text-fg-subtle">
              After each closed trade
            </span>
          }
        />
        <CardBody>
          <EquityCurve points={curves.equity} currency={currency} />
        </CardBody>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Profit by instrument" />
          <CardBody>
            <BarBreakdown buckets={by_symbol} label="Best and worst instruments" />
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Profit by hour of day" />
          <CardBody>
            <BarBreakdown
              buckets={by_hour}
              label="In your timezone"
              formatKey={formatHour}
            />
          </CardBody>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <SecondaryStat
          label="Profit factor"
          value={summary.profit_factor ? num(summary.profit_factor, 2) : "—"}
          hint={
            summary.profit_factor
              ? "Money won for every 1 lost"
              : "No losing trades yet, so this is undefined"
          }
        />
        <SecondaryStat
          label="Average win / loss"
          value={`${num(summary.avg_win)} / ${num(summary.avg_loss)}`}
          hint={`Longest streak: ${summary.longest_win_streak}W, ${summary.longest_loss_streak}L`}
        />
        <SecondaryStat
          label="Trades without a stop"
          value={String(summary.trades_without_stop)}
          hint={
            summary.trades_without_stop > 0
              ? "These are excluded from R and expectancy"
              : "Every trade had a stop loss"
          }
          tone={summary.trades_without_stop > 0 ? "warn" : undefined}
        />
      </div>

      <Card>
        <CardHeader title="Discipline" />
        <CardBody className="grid gap-4 sm:grid-cols-2">
          <Comparison
            title="After a losing trade"
            left={{ label: "Follows a loss", ...behaviour.after_a_loss.after_loss }}
            right={{ label: "Everything else", ...behaviour.after_a_loss.otherwise }}
            note="A large gap here is revenge trading with a number attached."
          />
          <Comparison
            title="Followed your plan"
            left={{ label: "Followed", ...behaviour.plan_adherence.followed }}
            right={{ label: "Deviated", ...behaviour.plan_adherence.deviated }}
            note={
              behaviour.plan_adherence.unjournalled > 0
                ? `${behaviour.plan_adherence.unjournalled} trades not written up yet.`
                : "Every trade is written up."
            }
          />
        </CardBody>
      </Card>
    </>
  );
}

function Headline({
  label, value, sub, tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "good" | "bad";
}) {
  return (
    <Card className="p-4">
      <p className="text-2xs font-semibold uppercase tracking-wider text-fg-subtle">
        {label}
      </p>
      <p
        className={cn(
          "mt-1.5 text-3xl font-semibold leading-none",
          tone === "good" && "text-long",
          tone === "bad" && "text-short",
        )}
      >
        {value}
      </p>
      {sub ? <p className="mt-2 text-xs text-fg-muted">{sub}</p> : null}
    </Card>
  );
}

function SecondaryStat({
  label, value, hint, tone,
}: {
  label: string;
  value: string;
  hint: string;
  tone?: "warn";
}) {
  return (
    <Card className="p-4">
      <p className="text-2xs font-semibold uppercase tracking-wider text-fg-subtle">
        {label}
      </p>
      <p className={cn("tabular mt-1.5 text-xl font-semibold", tone === "warn" && "text-warn")}>
        {value}
      </p>
      <p className="mt-1.5 text-xs text-fg-muted">{hint}</p>
    </Card>
  );
}

function Comparison({
  title, left, right, note,
}: {
  title: string;
  left: { label: string; trades: number; net_profit: string; expectancy_r: string | null };
  right: { label: string; trades: number; net_profit: string; expectancy_r: string | null };
  note: string;
}) {
  return (
    <div>
      <p className="mb-2 text-xs font-medium">{title}</p>
      <div className="grid grid-cols-2 gap-2">
        {[left, right].map((side) => {
          const value = Number.parseFloat(side.net_profit);
          return (
            <div key={side.label} className="rounded-md border border-line px-3 py-2">
              <p className="text-2xs text-fg-subtle">{side.label}</p>
              <p
                className={cn(
                  "tabular text-lg font-semibold",
                  value > 0 && "text-long",
                  value < 0 && "text-short",
                )}
              >
                {signed(side.net_profit)}
              </p>
              <p className="text-2xs text-fg-subtle">
                {side.trades} trade{side.trades === 1 ? "" : "s"}
                {side.expectancy_r ? ` · ${signed(side.expectancy_r, 2)}R` : ""}
              </p>
            </div>
          );
        })}
      </div>
      <p className="mt-1.5 text-2xs text-fg-subtle">{note}</p>
    </div>
  );
}
