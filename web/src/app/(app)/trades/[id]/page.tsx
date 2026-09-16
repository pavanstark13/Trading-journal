"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { JournalEditor } from "@/components/app/journal-editor";
import {
  Badge, Card, CardBody, CardHeader, ErrorNote, SideBadge, Skeleton, Table, Td, Th,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { TradeDetail } from "@/lib/types";
import { cn, formatTime, num, signed } from "@/lib/utils";

export default function TradeDetailPage() {
  const params = useParams<{ id: string }>();
  const { data, isLoading, error } = useQuery({
    queryKey: ["trade", params.id],
    queryFn: () => api.get<TradeDetail>(`/trades/${params.id}`),
  });

  if (error) return <ErrorNote error={error} />;
  if (isLoading || !data) return <Skeleton className="h-96" />;

  const profit = Number.parseFloat(data.net_profit);
  const r = data.r_multiple ? Number.parseFloat(data.r_multiple) : null;

  return (
    <>
      <Link
        href="/trades"
        className="inline-flex items-center gap-1.5 text-xs text-fg-muted hover:text-fg"
      >
        <ArrowLeft className="h-3.5 w-3.5" /> All trades
      </Link>

      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            {data.symbol}
            <SideBadge side={data.direction === "long" ? "BUY" : "SELL"} />
            {data.status === "open" ? <Badge tone="info">still open</Badge> : null}
          </h1>
          <p className="mt-0.5 text-sm text-fg-muted">
            {formatTime(data.opened_at)}
            {data.closed_at ? ` → ${formatTime(data.closed_at)}` : ""}
            {data.session ? ` · ${data.session} session` : ""}
          </p>
        </div>

        <div className="text-right">
          <p
            className={cn(
              "tabular text-3xl font-semibold leading-none",
              profit > 0 && "text-long",
              profit < 0 && "text-short",
            )}
          >
            {signed(data.net_profit)}
          </p>
          <p className="mt-1 text-xs text-fg-muted">
            {data.currency}
            {r !== null ? ` · ${signed(r, 2)}R` : " · no stop, so no R"}
            {data.pl_provisional ? " · still settling" : ""}
          </p>
        </div>
      </div>

      {data.pl_provisional && data.status === "closed" ? (
        <p className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn">
          Your broker can still book swap or commission against this trade for a few
          days, so the figure above may change slightly.
        </p>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[1fr_22rem]">
        <Card>
          <CardHeader
            title="Your notes"
            action={
              data.journal ? (
                <span className="text-2xs text-fg-subtle">
                  Updated {formatTime(data.journal.updated_at)}
                </span>
              ) : (
                <Badge tone="warn">Not written up</Badge>
              )
            }
          />
          <CardBody>
            <JournalEditor tradeId={data.id} journal={data.journal} />
          </CardBody>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHeader title="The trade" />
            <CardBody className="space-y-2 text-sm">
              <Row label="Size" value={`${num(data.volume_opened)} lots`} />
              <Row label="Entry" value={num(data.avg_entry_price, 5)} />
              <Row label="Exit" value={num(data.avg_exit_price, 5)} />
              <Row
                label="Stop loss"
                value={data.initial_sl ? num(data.initial_sl, 5) : "none set"}
                muted={!data.initial_sl}
              />
              <Row
                label="Take profit"
                value={data.initial_tp ? num(data.initial_tp, 5) : "none set"}
                muted={!data.initial_tp}
              />
              <Row label="Pips" value={data.pips ? signed(data.pips, 1) : "—"} />
              <Row
                label="Held for"
                value={data.duration_seconds ? humanDuration(data.duration_seconds) : "—"}
              />
              <Row label="Closed by" value={exitLabel(data.exit_reason)} />
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Costs" />
            <CardBody className="space-y-2 text-sm">
              <Row label="Gross" value={signed(data.gross_profit)} />
              <Row label="Commission" value={num(data.commission)} />
              <Row label="Swap" value={num(data.swap)} />
              <div className="flex items-center justify-between border-t border-line pt-2 font-medium">
                <span>Net</span>
                <span className="tabular">{signed(data.net_profit)}</span>
              </div>
              {data.risk_amount ? (
                <p className="pt-1 text-2xs text-fg-subtle">
                  You risked {num(data.risk_amount)} on this trade, so the result is{" "}
                  {signed(r ?? 0, 2)}R.
                </p>
              ) : null}
            </CardBody>
          </Card>
        </div>
      </div>

      <Card>
        <CardHeader
          title="Broker records"
          action={
            <span className="text-2xs text-fg-subtle">
              Every number above comes from these
            </span>
          }
        />
        <Table>
          <thead>
            <tr>
              <Th>#</Th>
              <Th>Type</Th>
              <Th>Ticket</Th>
              <Th className="text-right">Volume</Th>
              <Th className="text-right">Price</Th>
              <Th>Time</Th>
            </tr>
          </thead>
          <tbody>
            {data.legs.map((leg) => (
              <tr key={leg.seq}>
                <Td className="tabular text-fg-subtle">{leg.seq + 1}</Td>
                <Td>
                  <Badge tone={leg.type === "entry" ? "info" : "neutral"}>{leg.type}</Badge>
                </Td>
                <Td className="tabular text-xs">{leg.deal_ticket}</Td>
                <Td className="tabular text-right">{num(leg.volume)}</Td>
                <Td className="tabular text-right">{num(leg.price, 5)}</Td>
                <Td className="tabular text-xs text-fg-muted">
                  {formatTime(new Date(leg.time_msc).toISOString())}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>
    </>
  );
}

function Row({
  label, value, muted,
}: {
  label: string;
  value: string;
  muted?: boolean;
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-fg-muted">{label}</span>
      <span className={cn("tabular", muted && "text-fg-subtle")}>{value}</span>
    </div>
  );
}

function exitLabel(reason: string | null): string {
  return (
    { tp: "take profit", sl: "stop loss", so: "margin call", manual: "closed by hand" } as const
  )[reason ?? ""] ?? "—";
}

function humanDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)} days`;
}
