"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import {
  Badge, Card, EmptyState, ErrorNote, Input, Select, SideBadge,
  Skeleton, Table, Td, Th,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { TradeRow } from "@/lib/types";
import { cn, formatTime, num, signed } from "@/lib/utils";

export default function TradesPage() {
  const router = useRouter();
  const [symbol, setSymbol] = React.useState("");
  const [outcome, setOutcome] = React.useState("");
  const [journalled, setJournalled] = React.useState("");

  const symbols = useQuery({
    queryKey: ["symbols"],
    queryFn: () => api.get<string[]>("/trades/symbols"),
  });

  const params = new URLSearchParams({ limit: "200" });
  if (symbol) params.set("symbol", symbol);
  if (outcome) params.set("outcome", outcome);
  if (journalled) params.set("has_journal", journalled);

  const { data, isLoading, error } = useQuery({
    queryKey: ["trades", symbol, outcome, journalled],
    queryFn: () => api.get<{ items: TradeRow[]; total: number }>(`/trades?${params}`),
  });

  const unwritten = (data?.items ?? []).filter((t) => !t.has_journal).length;

  return (
    <>
      <PageHeader
        title="Trades"
        description={
          data
            ? `${data.total} trades · ${unwritten} still to write up`
            : undefined
        }
      />

      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
          <Select
            className="h-8 w-40"
            value={symbol}
            onChange={(event) => setSymbol(event.target.value)}
          >
            <option value="">All instruments</option>
            {(symbols.data ?? []).map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </Select>
          <Select
            className="h-8 w-36"
            value={outcome}
            onChange={(event) => setOutcome(event.target.value)}
          >
            <option value="">Wins and losses</option>
            <option value="win">Winners only</option>
            <option value="loss">Losers only</option>
          </Select>
          <Select
            className="h-8 w-44"
            value={journalled}
            onChange={(event) => setJournalled(event.target.value)}
          >
            <option value="">Written up or not</option>
            <option value="false">Not written up</option>
            <option value="true">Written up</option>
          </Select>
        </div>

        {error ? (
          <div className="p-4"><ErrorNote error={error} /></div>
        ) : isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-9" />)}
          </div>
        ) : !data || data.items.length === 0 ? (
          <EmptyState
            title="No trades match"
            hint="Trades appear here automatically once a MetaTrader account is connected."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Opened</Th>
                <Th>Instrument</Th>
                <Th>Side</Th>
                <Th className="text-right">Size</Th>
                <Th className="text-right">Entry</Th>
                <Th className="text-right">Exit</Th>
                <Th className="text-right">R</Th>
                <Th className="text-right">Result</Th>
                <Th>Note</Th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((trade) => {
                const profit = Number.parseFloat(trade.net_profit);
                return (
                  <tr
                    key={trade.id}
                    onClick={() => router.push(`/trades/${trade.id}`)}
                    className="cursor-pointer hover:bg-bg-sunken/70"
                  >
                    <Td className="tabular whitespace-nowrap text-xs text-fg-muted">
                      {formatTime(trade.opened_at)}
                    </Td>
                    <Td className="font-medium">
                      {trade.symbol}
                      {trade.status === "open" ? (
                        <Badge tone="info" className="ml-1.5">open</Badge>
                      ) : null}
                    </Td>
                    <Td>
                      <SideBadge side={trade.direction === "long" ? "BUY" : "SELL"} />
                    </Td>
                    <Td className="tabular text-right">{num(trade.volume_opened)}</Td>
                    <Td className="tabular text-right text-fg-muted">
                      {num(trade.avg_entry_price, 5)}
                    </Td>
                    <Td className="tabular text-right text-fg-muted">
                      {num(trade.avg_exit_price, 5)}
                    </Td>
                    <Td className="tabular text-right">
                      {trade.r_multiple ? (
                        signed(trade.r_multiple, 2)
                      ) : (
                        <span
                          className="text-fg-subtle"
                          title="No stop loss on this trade, so R cannot be measured"
                        >
                          —
                        </span>
                      )}
                    </Td>
                    <Td
                      className={cn(
                        "tabular text-right font-medium",
                        profit > 0 && "text-long",
                        profit < 0 && "text-short",
                      )}
                    >
                      {signed(trade.net_profit)}
                    </Td>
                    <Td>
                      {trade.has_journal ? (
                        <Badge tone="good">written</Badge>
                      ) : (
                        <Badge tone="neutral">—</Badge>
                      )}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}
