"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import { Dialog } from "@/components/ui/dialog";
import {
  Badge, Button, Card, ConnectionDot, EmptyState, ErrorNote,
  Skeleton, StatusBadge, Table, Td, Th,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { EaInstallationRow } from "@/lib/types";
import { relativeTime, shortId } from "@/lib/utils";

export default function EaInstallationsPage() {
  const queryClient = useQueryClient();
  const [rotated, setRotated] = React.useState<{ key: string; secret: string } | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["ea-installations"],
    queryFn: () => api.get<EaInstallationRow[]>("/ea-installations"),
    refetchInterval: 20_000,
  });

  const rotate = useMutation({
    mutationFn: (id: string) =>
      api.post<{ api_key_id: string; api_secret: string }>(
        `/ea-installations/${id}/rotate-secret`,
      ),
    onSuccess: (result) =>
      setRotated({ key: result.api_key_id, secret: result.api_secret }),
  });

  const revoke = useMutation({
    mutationFn: (id: string) => api.post(`/ea-installations/${id}/revoke`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["ea-installations"] }),
  });

  return (
    <>
      <PageHeader
        title="EA installations"
        description="Every terminal authorized to talk to this system."
      />

      {rotate.error ? <ErrorNote error={rotate.error} /> : null}

      <Card>
        {error ? (
          <div className="p-4"><ErrorNote error={error} /></div>
        ) : isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 3 }).map((_, index) => (
              <Skeleton key={index} className="h-10" />
            ))}
          </div>
        ) : !data || data.length === 0 ? (
          <EmptyState title="No installations registered" />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Kind</Th>
                <Th>Key</Th>
                <Th>Status</Th>
                <Th>Connection</Th>
                <Th>EA version</Th>
                <Th>Build</Th>
                <Th>Last seen</Th>
                <Th>IP</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {data.map((install) => (
                <tr key={install.id} className="hover:bg-bg-sunken/60">
                  <Td>
                    <Badge tone={install.kind === "MASTER" ? "info" : "neutral"}>
                      {install.kind}
                    </Badge>
                  </Td>
                  <Td className="font-mono text-xs">{shortId(install.id, 10)}</Td>
                  <Td>
                    <StatusBadge status={install.status} />
                    {install.pending_install_code ? (
                      <Badge tone="warn" className="ml-1.5">code issued</Badge>
                    ) : null}
                  </Td>
                  <Td><ConnectionDot status={install.connection} label={install.connection} /></Td>
                  <Td className="text-xs">{install.ea_version ?? "—"}</Td>
                  <Td className="tabular text-xs">{install.terminal_build ?? "—"}</Td>
                  <Td className="text-xs text-fg-muted">{relativeTime(install.last_seen_at)}</Td>
                  <Td className="font-mono text-2xs text-fg-subtle">
                    {install.last_seen_ip ?? "—"}
                  </Td>
                  <Td>
                    <div className="flex justify-end gap-1.5">
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={install.status !== "ACTIVE"}
                        onClick={() => rotate.mutate(install.id)}
                      >
                        Rotate secret
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="text-danger"
                        disabled={install.status === "REVOKED"}
                        onClick={() => revoke.mutate(install.id)}
                      >
                        Revoke
                      </Button>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <p className="text-xs text-fg-subtle">
        Rotating a secret keeps the previous one valid for 24 hours, so a terminal can be
        updated without an outage. Revoking takes effect on the next request.
      </p>

      <Dialog
        open={Boolean(rotated)}
        onClose={() => {
          setRotated(null);
          queryClient.invalidateQueries({ queryKey: ["ea-installations"] });
        }}
        title="New EA secret"
        description="Shown once. Paste it into the terminal within 24 hours; the previous secret stops working after that."
      >
        <div className="space-y-2">
          <div>
            <p className="text-2xs uppercase tracking-wider text-fg-subtle">Key id</p>
            <code className="block break-all rounded bg-bg-sunken px-3 py-2 font-mono text-xs">
              {rotated?.key}
            </code>
          </div>
          <div>
            <p className="text-2xs uppercase tracking-wider text-fg-subtle">Secret</p>
            <code className="block break-all rounded bg-bg-sunken px-3 py-2 font-mono text-xs">
              {rotated?.secret}
            </code>
          </div>
        </div>
      </Dialog>
    </>
  );
}
