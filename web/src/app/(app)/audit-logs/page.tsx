"use client";

import { useQuery } from "@tanstack/react-query";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import {
  Badge, Card, EmptyState, ErrorNote, Input, Skeleton, Table, Td, Th,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { AuditRow } from "@/lib/types";
import { formatTime, shortId } from "@/lib/utils";

/** Actions worth colouring: the ones an incident review looks for first. */
const SIGNIFICANT = new Set([
  "EMERGENCY_STOP", "EMERGENCY_STOP_CLEARED", "MODE_CHANGED",
  "EA_SECRET_ROTATED", "EA_INSTALLATION_REVOKED", "REFRESH_REUSE_DETECTED",
]);

export default function AuditLogsPage() {
  const [action, setAction] = React.useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["audit-logs", action],
    queryFn: () =>
      api.get<{ items: AuditRow[] }>(
        `/audit-logs?limit=200${action ? `&action=${encodeURIComponent(action)}` : ""}`,
      ),
  });

  return (
    <>
      <PageHeader
        title="Audit log"
        description="Append-only record of every privileged action. Retained seven years."
      />

      <Card>
        <div className="border-b border-line px-4 py-3">
          <Input
            className="h-8 w-64"
            placeholder="Filter by action…"
            value={action}
            onChange={(event) => setAction(event.target.value.toUpperCase())}
          />
        </div>

        {error ? (
          <div className="p-4"><ErrorNote error={error} /></div>
        ) : isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 8 }).map((_, index) => (
              <Skeleton key={index} className="h-8" />
            ))}
          </div>
        ) : !data || data.items.length === 0 ? (
          <EmptyState title="No audit entries match" />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Time</Th>
                <Th>Action</Th>
                <Th>Actor</Th>
                <Th>Entity</Th>
                <Th>Change</Th>
                <Th>IP</Th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((row) => (
                <tr key={row.id} className="hover:bg-bg-sunken/60">
                  <Td className="tabular whitespace-nowrap text-xs text-fg-muted">
                    {formatTime(row.at)}
                  </Td>
                  <Td>
                    <Badge tone={SIGNIFICANT.has(row.action) ? "bad" : "neutral"}>
                      {row.action.replace(/_/g, " ")}
                    </Badge>
                  </Td>
                  <Td className="font-mono text-xs">
                    {row.actor_type === "USER"
                      ? shortId(row.actor_user_id)
                      : row.actor_type}
                  </Td>
                  <Td className="text-xs text-fg-muted">
                    {row.entity_type
                      ? `${row.entity_type} ${shortId(row.entity_id, 8)}`
                      : "—"}
                  </Td>
                  <Td className="max-w-[24rem] truncate text-2xs text-fg-subtle">
                    {row.after ? JSON.stringify(row.after) : "—"}
                  </Td>
                  <Td className="font-mono text-2xs text-fg-subtle">{row.ip ?? "—"}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}
