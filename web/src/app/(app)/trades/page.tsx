"use client";

import { useQuery } from "@tanstack/react-query";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import {
  Badge, Card, EmptyState, ErrorNote, Input, Select, SideBadge,
  Skeleton, StatusBadge, Table, Td, Th,
} from "@/components/ui/primitives";
import { TradeDetail } from "@/components/app/trade-detail";
import { api } from "@/lib/api";
import type { TradeEventRow } from "@/lib/types";
import { formatTime, num } from "@/lib/utils";

const EVENT_TYPES = [
  "TRADE_OPENED", "TRADE_MODIFIED", "TRADE_CLOSED", "TRADE_PARTIAL_CLOSED",
  "PENDING_ORDER_CREATED", "PENDING_ORDER_MODIFIED", "PENDING_ORDER_CANCELLED",
  "PENDING_ORDER_TRIGGERED",
];

export default function TradesPage() {
  const [symbol, setSymbol] = React.useState("");
  const [eventType, setEventType] = React.useState("");
  const [selected, setSelected] = React.useState<string | null>(null);

  const query = new URLSearchParams();
  if (symbol) query.set("symbol", symbol);
  if (eventType) query.set("event_type", eventType);

  const { data, isLoading, error } = useQuery({
    queryKey: ["trades", symbol, eventType],
    queryFn: () =>
      api.get<{ items: TradeEventRow[] }>(`/trades?${query.toString()}`),
  });

  return (
    <>
      <PageHeader
        title="Trade events"
        description="Every transaction the master EA reported, and what happened to it."
      />

      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
          <Input
            placeholder="Filter symbol…"
            className="h-8 w-40"
            value={symbol}
            onChange={(event) => setSymbol(event.target.value.toUpperCase())}
          />
          <Select
            className="h-8 w-56"
            value={eventType}
            onChange={(event) => setEventType(event.target.value)}
          >
            <option value="">All event types</option>
            {EVENT_TYPES.map((type) => (
              <option key={type} value={type}>
                {type.replace(/_/g, " ")}
              </option>
            ))}
          </Select>
        </div>

        {error ? (
          <div className="p-4"><ErrorNote error={error} /></div>
        ) : isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 6 }).map((_, index) => (
              <Skeleton key={index} className="h-9" />
            ))}
          </div>
        ) : !data || data.items.length === 0 ? (
          <EmptyState
            title="No trade events match"
            hint="Events arrive within a second of a master fill."
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
                <Th className="text-right">SL</Th>
                <Th className="text-right">TP</Th>
                <Th>Telegram</Th>
                <Th>Copies</Th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((event) => (
                <tr
                  key={event.id}
                  onClick={() => setSelected(event.id)}
                  className="cursor-pointer hover:bg-bg-sunken/60"
                >
                  <Td className="tabular whitespace-nowrap text-xs text-fg-muted">
                    {formatTime(event.occurred_at)}
                  </Td>
                  <Td className="text-xs">{event.event_type.replace(/_/g, " ")}</Td>
                  <Td className="font-medium">{event.symbol ?? "—"}</Td>
                  <Td><SideBadge side={event.side} /></Td>
                  <Td className="tabular text-right">{num(event.volume)}</Td>
                  <Td className="tabular text-right">{num(event.price, 5)}</Td>
                  <Td className="tabular text-right text-fg-muted">
                    {num(event.stop_loss, 5)}
                  </Td>
                  <Td className="tabular text-right text-fg-muted">
                    {num(event.take_profit, 5)}
                  </Td>
                  <Td>
                    <StatusBadge status={event.telegram_status ?? "NONE"} />
                  </Td>
                  <Td>
                    {event.copy_summary && event.copy_summary.total > 0 ? (
                      <span className="flex items-center gap-1">
                        <Badge tone="good">{event.copy_summary.executed}</Badge>
                        {event.copy_summary.failed > 0 ? (
                          <Badge tone="bad">{event.copy_summary.failed}</Badge>
                        ) : null}
                        {event.copy_summary.rejected > 0 ? (
                          <Badge tone="neutral">{event.copy_summary.rejected}</Badge>
                        ) : null}
                      </span>
                    ) : (
                      <span className="text-xs text-fg-subtle">—</span>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <TradeDetail eventId={selected} onClose={() => setSelected(null)} />
    </>
  );
}
