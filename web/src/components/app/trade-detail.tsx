"use client";

import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, CircleDot, XCircle } from "lucide-react";

import { Dialog } from "@/components/ui/dialog";
import {
  Badge, ErrorNote, Skeleton, StatusBadge, Table, Td, Th,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { TimelineEntry, TradeEventRow } from "@/lib/types";
import { formatTime, num, shortId } from "@/lib/utils";

interface TradeDetailData extends TradeEventRow {
  raw_payload: Record<string, unknown>;
  copy_orders: {
    id: string;
    member: string;
    status: string;
    requested_lot: string;
    final_lot: string;
    execution_price: string | null;
    slippage_points: string | null;
    broker_ticket: number | null;
    reject_reason: string | null;
    reject_detail: string | null;
    latency_ms: number | null;
    is_paper: boolean;
  }[];
  timeline: TimelineEntry[];
}

/** The nine canonical stages, so a missing one is visible as a gap, not an absence. */
const STAGES = [
  "MASTER_EVENT", "API_RECEIVED", "DB_STORED", "QUEUED",
  "TELEGRAM_PUBLISHED", "COPY_PLANNED", "COPY_DISPATCHED",
  "BROKER_EXECUTION", "RESULT_RECEIVED",
];

export function TradeDetail({
  eventId,
  onClose,
}: {
  eventId: string | null;
  onClose: () => void;
}) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["trade", eventId],
    queryFn: () => api.get<TradeDetailData>(`/trades/${eventId}`),
    enabled: Boolean(eventId),
  });

  const reached = new Map(data?.timeline.map((entry) => [entry.stage, entry]) ?? []);

  return (
    <Dialog
      open={Boolean(eventId)}
      onClose={onClose}
      title={data ? `${data.symbol ?? ""} ${data.event_type.replace(/_/g, " ")}` : "Trade"}
      description={data ? `Event ${shortId(data.event_id, 12)}` : undefined}
    >
      <div className="max-h-[70vh] w-[min(88vw,52rem)] overflow-y-auto">
        {error ? <ErrorNote error={error} /> : null}
        {isLoading ? <Skeleton className="h-64" /> : null}

        {data ? (
          <div className="space-y-5">
            <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
              {[
                ["Side", data.side ?? "—"],
                ["Volume", num(data.volume)],
                ["Price", num(data.price, 5)],
                ["Stop loss", num(data.stop_loss, 5)],
                ["Take profit", num(data.take_profit, 5)],
                ["Occurred", formatTime(data.occurred_at)],
                ["Received", formatTime(data.received_at)],
                ["Processing", data.processing_status],
              ].map(([label, value]) => (
                <div key={label}>
                  <dt className="text-2xs uppercase tracking-wider text-fg-subtle">{label}</dt>
                  <dd className="tabular mt-0.5">{value}</dd>
                </div>
              ))}
            </dl>

            <section>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-fg-subtle">
                Lifecycle
              </h3>
              <ol className="space-y-1.5">
                {STAGES.map((stage) => {
                  const entry = reached.get(stage);
                  const failed = entry?.status === "FAIL";
                  return (
                    <li key={stage} className="flex items-start gap-2.5 text-sm">
                      {failed ? (
                        <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
                      ) : entry ? (
                        <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-long" />
                      ) : (
                        <CircleDot className="mt-0.5 h-4 w-4 shrink-0 text-fg-subtle/40" />
                      )}
                      <div className="min-w-0 flex-1">
                        <span className={entry ? "" : "text-fg-subtle"}>
                          {stage.replace(/_/g, " ")}
                        </span>
                        {entry?.message ? (
                          <p className="text-xs text-fg-muted">{entry.message}</p>
                        ) : null}
                      </div>
                      {entry ? (
                        <span className="tabular shrink-0 text-2xs text-fg-subtle">
                          {formatTime(entry.at)}
                        </span>
                      ) : null}
                    </li>
                  );
                })}
              </ol>
            </section>

            <section>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-fg-subtle">
                Copy outcomes
              </h3>
              {data.copy_orders.length === 0 ? (
                <p className="text-sm text-fg-muted">
                  No copy orders were created for this event.
                </p>
              ) : (
                <Table>
                  <thead>
                    <tr>
                      <Th>Member</Th>
                      <Th>Status</Th>
                      <Th className="text-right">Lot</Th>
                      <Th className="text-right">Fill</Th>
                      <Th className="text-right">Slip</Th>
                      <Th className="text-right">Latency</Th>
                      <Th>Reason</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.copy_orders.map((order) => (
                      <tr key={order.id}>
                        <Td>
                          {order.member}
                          {order.is_paper ? (
                            <Badge tone="info" className="ml-1.5">paper</Badge>
                          ) : null}
                        </Td>
                        <Td><StatusBadge status={order.status} /></Td>
                        <Td className="tabular text-right">{num(order.final_lot)}</Td>
                        <Td className="tabular text-right">{num(order.execution_price, 5)}</Td>
                        <Td className="tabular text-right">{num(order.slippage_points, 1)}</Td>
                        <Td className="tabular text-right text-fg-muted">
                          {order.latency_ms ? `${order.latency_ms}ms` : "—"}
                        </Td>
                        <Td className="max-w-[16rem] truncate text-xs text-fg-muted">
                          {order.reject_detail ?? order.reject_reason ?? "—"}
                        </Td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              )}
            </section>

            <details className="text-xs">
              <summary className="cursor-pointer text-fg-muted">
                Raw payload from the EA
              </summary>
              <pre className="mt-2 overflow-x-auto rounded bg-bg-sunken p-3 text-2xs">
                {JSON.stringify(data.raw_payload, null, 2)}
              </pre>
            </details>
          </div>
        ) : null}
      </div>
    </Dialog>
  );
}
