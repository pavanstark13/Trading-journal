"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import {
  Badge, Button, Card, EmptyState, ErrorNote, Select,
  SideBadge, Skeleton, StatusBadge, Table, Td, Th,
} from "@/components/ui/primitives";
import { useAuth } from "@/hooks/useAuth";
import { api } from "@/lib/api";
import type { CopyOrderRow } from "@/lib/types";
import { formatTime, num, shortId } from "@/lib/utils";

const STATUSES = [
  "PENDING", "SENT", "EXECUTED", "FAILED", "REJECTED", "CANCELLED", "TIMED_OUT",
];

export default function CopyOrdersPage() {
  const [status, setStatus] = React.useState("");
  const { can } = useAuth();
  const queryClient = useQueryClient();

  const params = new URLSearchParams();
  if (status) params.set("order_status", status);

  const { data, isLoading, error } = useQuery({
    queryKey: ["copy-orders", status],
    queryFn: () => api.get<{ items: CopyOrderRow[] }>(`/copy/orders?${params}`),
  });

  const retry = useMutation({
    mutationFn: (id: string) => api.post(`/copy/orders/${id}/retry`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["copy-orders"] }),
  });

  return (
    <>
      <PageHeader
        title="Copy orders"
        description="Every instruction planned for a member, including the ones that were blocked."
      />

      <Card>
        <div className="flex items-center gap-2 border-b border-line px-4 py-3">
          <Select
            className="h-8 w-48"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            <option value="">All statuses</option>
            {STATUSES.map((value) => (
              <option key={value} value={value}>{value.replace(/_/g, " ")}</option>
            ))}
          </Select>
          {retry.error ? <ErrorNote error={retry.error} /> : null}
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
            title="No copy orders yet"
            hint="A copy order is created for every active member on every master trade — including rejections."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Created</Th>
                <Th>Member</Th>
                <Th>Action</Th>
                <Th>Symbol</Th>
                <Th>Side</Th>
                <Th className="text-right">Requested</Th>
                <Th className="text-right">Final lot</Th>
                <Th className="text-right">Fill</Th>
                <Th className="text-right">Slip</Th>
                <Th>Status</Th>
                <Th>Reason</Th>
                {can("ADMIN") ? <Th /> : null}
              </tr>
            </thead>
            <tbody>
              {data.items.map((order) => (
                <tr key={order.id} className="hover:bg-bg-sunken/60">
                  <Td className="tabular whitespace-nowrap text-xs text-fg-muted">
                    {formatTime(order.created_at)}
                  </Td>
                  <Td className="font-mono text-xs">
                    {shortId(order.member_account_id)}
                    {order.is_paper ? (
                      <Badge tone="info" className="ml-1.5">paper</Badge>
                    ) : null}
                  </Td>
                  <Td className="text-xs">{order.action}</Td>
                  <Td className="font-medium">{order.symbol}</Td>
                  <Td><SideBadge side={order.side} /></Td>
                  <Td className="tabular text-right text-fg-muted">
                    {num(order.requested_lot)}
                  </Td>
                  <Td className="tabular text-right font-medium">{num(order.final_lot)}</Td>
                  <Td className="tabular text-right">{num(order.execution_price, 5)}</Td>
                  <Td className="tabular text-right">{num(order.slippage_points, 1)}</Td>
                  <Td><StatusBadge status={order.status} /></Td>
                  <Td className="max-w-[18rem] truncate text-xs text-fg-muted">
                    {order.reject_detail ?? order.reject_reason ?? "—"}
                  </Td>
                  {can("ADMIN") ? (
                    <Td>
                      {order.status === "FAILED" || order.status === "TIMED_OUT" ? (
                        <Button
                          size="sm"
                          variant="secondary"
                          disabled={retry.isPending}
                          onClick={() => retry.mutate(order.id)}
                        >
                          Retry
                        </Button>
                      ) : null}
                    </Td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <p className="text-xs text-fg-subtle">
        A retry re-runs the full risk gate, including the staleness check — it is never a
        bypass. An order older than the member&apos;s maximum signal age will be rejected again.
      </p>
    </>
  );
}
