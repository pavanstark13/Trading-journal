"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { PageHeader } from "@/components/app/page-header";
import {
  Card, CardBody, CardHeader, ConnectionDot, EmptyState, ErrorNote,
  SideBadge, Skeleton, Stat, StatusBadge, Table, Td, Th,
} from "@/components/ui/primitives";
import { useLiveFeed } from "@/hooks/useLiveFeed";
import { api } from "@/lib/api";
import type { DashboardData } from "@/lib/types";
import { formatTime, num, relativeTime } from "@/lib/utils";

const COMPONENTS = [
  ["master_ea", "Master EA"],
  ["database", "Database"],
  ["redis", "Redis"],
  ["worker", "Worker"],
  ["telegram", "Telegram"],
] as const;

export default function DashboardPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api.get<DashboardData>("/admin/dashboard"),
    refetchInterval: 30_000,
  });
  const { events, health: liveHealth } = useLiveFeed();

  if (error) return <ErrorNote error={error} />;
  if (isLoading || !data) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 8 }).map((_, index) => (
          <Skeleton key={index} className="h-24" />
        ))}
      </div>
    );
  }

  const health = { ...data.health, ...liveHealth };
  const copyTotal =
    data.counts.copies_successful + data.counts.copies_failed + data.counts.copies_rejected;

  return (
    <>
      <PageHeader
        title="Operations overview"
        description="Live state of the bridge, the channel and every member copier."
      />

      {/* Component health reads left to right in the order failures cascade. */}
      <Card>
        <CardHeader title="System status" />
        <div className="grid divide-y divide-line sm:grid-cols-5 sm:divide-x sm:divide-y-0">
          {COMPONENTS.map(([key, label]) => (
            <div key={key} className="flex items-center gap-2.5 px-4 py-3">
              <ConnectionDot status={health[key] ?? "UNKNOWN"} />
              <div className="min-w-0">
                <p className="truncate text-xs font-medium">{label}</p>
                <p className="text-2xs text-fg-subtle">{health[key] ?? "UNKNOWN"}</p>
              </div>
            </div>
          ))}
        </div>
      </Card>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Master equity"
          value={data.master ? num(data.master.equity) : "—"}
          sub={
            data.master
              ? `Balance ${num(data.master.balance)} · ${data.master.open_positions ?? 0} open`
              : "No master account configured"
          }
        />
        <Stat
          label="Members connected"
          value={`${data.counts.members_connected}/${data.counts.members}`}
          sub="EA heartbeat within 90s"
          tone={
            data.counts.members > 0 && data.counts.members_connected === 0 ? "bad" : undefined
          }
        />
        <Stat
          label="Trades today"
          value={data.counts.trades_today}
          sub={
            data.master?.last_heartbeat_at
              ? `Last heartbeat ${relativeTime(data.master.last_heartbeat_at)}`
              : "Waiting for the master EA"
          }
        />
        <Stat
          label="Copies today"
          value={data.counts.copies_successful}
          sub={
            copyTotal === 0
              ? "No copy activity yet"
              : `${data.counts.copies_failed} failed · ${data.counts.copies_rejected} rejected`
          }
          tone={data.counts.copies_failed > 0 ? "warn" : undefined}
        />
      </div>

      {data.counts.dead_letters > 0 ? (
        <Card className="border-danger/40 bg-danger/5">
          <CardBody className="flex items-center justify-between gap-3">
            <div>
              <p className="text-sm font-medium text-danger">
                {data.counts.dead_letters} message(s) in the dead-letter queue
              </p>
              <p className="mt-0.5 text-xs text-fg-muted">
                These were retried to exhaustion and never delivered. They need a decision.
              </p>
            </div>
            <Link href="/system-health" className="text-xs font-medium text-accent underline">
              Review
            </Link>
          </CardBody>
        </Card>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader
            title="Recent master events"
            action={
              <Link href="/trades" className="text-xs text-accent hover:underline">
                All trades →
              </Link>
            }
          />
          {data.recent_events.length === 0 ? (
            <EmptyState
              title="No events received yet"
              hint="Once the master EA is attached and a trade is taken, it appears here within a second."
            />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Time</Th>
                  <Th>Event</Th>
                  <Th>Symbol</Th>
                  <Th>Side</Th>
                  <Th className="text-right">Volume</Th>
                  <Th className="text-right">Price</Th>
                  <Th>Status</Th>
                </tr>
              </thead>
              <tbody>
                {data.recent_events.map((event) => (
                  <tr key={event.id} className="hover:bg-bg-sunken/60">
                    <Td className="tabular whitespace-nowrap text-xs text-fg-muted">
                      {formatTime(event.occurred_at)}
                    </Td>
                    <Td className="text-xs">{event.event_type.replace(/_/g, " ")}</Td>
                    <Td className="font-medium">{event.symbol ?? "—"}</Td>
                    <Td><SideBadge side={event.side} /></Td>
                    <Td className="tabular text-right">{num(event.volume)}</Td>
                    <Td className="tabular text-right">{num(event.price, 5)}</Td>
                    <Td><StatusBadge status={event.processing_status} /></Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>

        <Card>
          <CardHeader title="Live activity" />
          <div className="max-h-[420px] overflow-y-auto">
            {events.length === 0 ? (
              <EmptyState title="Listening…" hint="Events appear here as they arrive." />
            ) : (
              <ul className="divide-y divide-line/60">
                {events.map((event, index) => (
                  <li key={index} className="px-4 py-2 text-xs">
                    <span className="text-fg-subtle">{event.type}</span>
                    <p className="mt-0.5 truncate text-fg-muted">
                      {JSON.stringify(event.data)}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>
      </div>
    </>
  );
}
