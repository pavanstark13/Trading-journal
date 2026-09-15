"use client";

import { useQuery } from "@tanstack/react-query";

import { PageHeader } from "@/components/app/page-header";
import {
  Card, CardBody, CardHeader, ConnectionDot, EmptyState, ErrorNote,
  SideBadge, Skeleton, Stat, Table, Td, Th,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { MasterAccountRow } from "@/lib/types";
import { formatTime, num, relativeTime } from "@/lib/utils";

interface Position {
  position_id: number;
  symbol: string;
  side: string;
  volume: string;
  open_price: string;
  stop_loss: string | null;
  take_profit: string | null;
  opened_at: string;
}

export default function MasterAccountPage() {
  const accounts = useQuery({
    queryKey: ["master-accounts"],
    queryFn: () => api.get<MasterAccountRow[]>("/master/accounts"),
    refetchInterval: 20_000,
  });

  const accountId = accounts.data?.[0]?.id;

  const detail = useQuery({
    queryKey: ["master-account", accountId],
    queryFn: () => api.get<MasterAccountRow>(`/master/accounts/${accountId}`),
    enabled: Boolean(accountId),
    refetchInterval: 20_000,
  });

  const positions = useQuery({
    queryKey: ["master-positions", accountId],
    queryFn: () => api.get<Position[]>(`/master/accounts/${accountId}/positions`),
    enabled: Boolean(accountId),
    refetchInterval: 20_000,
  });

  if (accounts.error) return <ErrorNote error={accounts.error} />;
  if (accounts.isLoading) return <Skeleton className="h-64" />;

  if (!accounts.data || accounts.data.length === 0) {
    return (
      <>
        <PageHeader title="Master account" />
        <Card>
          <EmptyState
            title="No master account configured"
            hint="Create one with: docker compose exec backend python -m app.cli create-master --mt5-login … --server …"
          />
        </Card>
      </>
    );
  }

  const account = detail.data ?? accounts.data[0];

  return (
    <>
      <PageHeader
        title={account.label}
        description={`${account.mt5_login} @ ${account.broker_server} · ${account.margin_mode}`}
      />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Balance" value={num(account.balance)} sub={account.currency} />
        <Stat label="Equity" value={num(account.equity)} sub={account.currency} />
        <Stat label="Margin" value={num(account.margin)} sub={`Free ${num(account.free_margin)}`} />
        <Stat
          label="Open positions"
          value={account.open_positions ?? 0}
          sub={`Heartbeat ${relativeTime(account.last_heartbeat_at)}`}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader title="Bridge status" />
          <CardBody className="space-y-3 text-sm">
            <Row label="Connection">
              <ConnectionDot status={account.connection} label={account.connection} />
            </Row>
            <Row label="EA status">{account.ea?.status ?? "not installed"}</Row>
            <Row label="EA version">{account.ea?.ea_version ?? "—"}</Row>
            <Row label="Terminal build">{account.ea?.terminal_build ?? "—"}</Row>
            <Row label="Last seen">{relativeTime(account.ea?.last_seen_at)}</Row>
            <Row label="Publishing">{account.publish_enabled ? "enabled" : "disabled"}</Row>
            <Row label="Copying">{account.copy_enabled ? "enabled" : "disabled"}</Row>
          </CardBody>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader title="Open positions" />
          {positions.data && positions.data.length > 0 ? (
            <Table>
              <thead>
                <tr>
                  <Th>Symbol</Th>
                  <Th>Side</Th>
                  <Th className="text-right">Volume</Th>
                  <Th className="text-right">Open</Th>
                  <Th className="text-right">SL</Th>
                  <Th className="text-right">TP</Th>
                  <Th>Opened</Th>
                </tr>
              </thead>
              <tbody>
                {positions.data.map((position) => (
                  <tr key={position.position_id}>
                    <Td className="font-medium">{position.symbol}</Td>
                    <Td><SideBadge side={position.side} /></Td>
                    <Td className="tabular text-right">{num(position.volume)}</Td>
                    <Td className="tabular text-right">{num(position.open_price, 5)}</Td>
                    <Td className="tabular text-right text-fg-muted">
                      {num(position.stop_loss, 5)}
                    </Td>
                    <Td className="tabular text-right text-fg-muted">
                      {num(position.take_profit, 5)}
                    </Td>
                    <Td className="tabular text-xs text-fg-muted">
                      {formatTime(position.opened_at)}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <EmptyState title="No open positions" />
          )}
        </Card>
      </div>

      {account.last_event ? (
        <p className="text-xs text-fg-subtle">
          Last event received: {account.last_event.event_type.replace(/_/g, " ")} on{" "}
          {account.last_event.symbol} at {formatTime(account.last_event.occurred_at)}.
        </p>
      ) : null}
    </>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-fg-muted">{label}</span>
      <span className="text-right">{children}</span>
    </div>
  );
}
