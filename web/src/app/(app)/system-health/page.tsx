"use client";

import { useQuery } from "@tanstack/react-query";

import { PageHeader } from "@/components/app/page-header";
import {
  Badge, Card, CardBody, CardHeader, ConnectionDot, EmptyState,
  ErrorNote, Skeleton, Table, Td, Th,
} from "@/components/ui/primitives";
import { useLiveFeed } from "@/hooks/useLiveFeed";
import { api } from "@/lib/api";
import { formatTime, num, relativeTime } from "@/lib/utils";

interface DetailedHealth {
  status: string;
  checked_at: string;
  components: Record<
    string,
    {
      status: string;
      latency_ms?: number;
      error?: string;
      checked_at?: string;
      total?: number;
      online?: number;
      items?: { installation_id: string; status: string; last_seen_at: string | null }[];
    }
  >;
}

interface DeadLetter {
  id: string;
  source: string;
  error: string;
  attempts: number;
  first_failed_at: string;
  last_failed_at: string;
}

export default function SystemHealthPage() {
  const { connected } = useLiveFeed();

  const health = useQuery({
    queryKey: ["health-detailed"],
    queryFn: () => api.get<DetailedHealth>("/health/detailed"),
    refetchInterval: 15_000,
  });

  const deadLetters = useQuery({
    queryKey: ["dead-letters"],
    queryFn: () => api.get<DeadLetter[]>("/admin/dead-letters"),
    refetchInterval: 30_000,
  });

  return (
    <>
      <PageHeader
        title="System health"
        description="Component state, EA connectivity and anything that failed past its retries."
      />

      <Card>
        <CardHeader
          title="Components"
          action={
            <ConnectionDot
              status={connected ? "ONLINE" : "OFFLINE"}
              label={connected ? "live stream connected" : "live stream down"}
            />
          }
        />
        {health.error ? (
          <CardBody><ErrorNote error={health.error} /></CardBody>
        ) : health.isLoading ? (
          <CardBody><Skeleton className="h-32" /></CardBody>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Component</Th>
                <Th>Status</Th>
                <Th className="text-right">Latency</Th>
                <Th>Checked</Th>
                <Th>Detail</Th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(health.data?.components ?? {}).map(([name, component]) => (
                <tr key={name}>
                  <Td className="font-medium">{name.replace(/_/g, " ")}</Td>
                  <Td><ConnectionDot status={component.status} label={component.status} /></Td>
                  <Td className="tabular text-right text-xs text-fg-muted">
                    {component.latency_ms !== undefined
                      ? `${num(component.latency_ms, 1)}ms`
                      : "—"}
                  </Td>
                  <Td className="tabular text-xs text-fg-muted">
                    {component.checked_at ? formatTime(component.checked_at) : "—"}
                  </Td>
                  <Td className="text-xs text-fg-muted">
                    {component.error
                      ? component.error
                      : component.total !== undefined
                        ? `${component.online}/${component.total} online`
                        : "—"}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {health.data?.components.member_eas?.items?.length ? (
        <Card>
          <CardHeader title="Member terminals" />
          <Table>
            <thead>
              <tr>
                <Th>Installation</Th>
                <Th>Status</Th>
                <Th>Last seen</Th>
              </tr>
            </thead>
            <tbody>
              {health.data.components.member_eas.items.map((item) => (
                <tr key={item.installation_id}>
                  <Td className="font-mono text-xs">{item.installation_id.slice(0, 8)}</Td>
                  <Td><ConnectionDot status={item.status} label={item.status} /></Td>
                  <Td className="text-xs text-fg-muted">{relativeTime(item.last_seen_at)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      ) : null}

      <Card className={deadLetters.data?.length ? "border-danger/40" : undefined}>
        <CardHeader
          title="Dead letter queue"
          action={
            deadLetters.data?.length ? (
              <Badge tone="bad">{deadLetters.data.length} unresolved</Badge>
            ) : null
          }
        />
        {!deadLetters.data || deadLetters.data.length === 0 ? (
          <EmptyState
            title="Nothing has been dead-lettered"
            hint="Messages land here only after exhausting every retry. They are never auto-deleted."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Source</Th>
                <Th className="text-right">Attempts</Th>
                <Th>First failed</Th>
                <Th>Last failed</Th>
                <Th>Error</Th>
              </tr>
            </thead>
            <tbody>
              {deadLetters.data.map((row) => (
                <tr key={row.id}>
                  <Td>{row.source}</Td>
                  <Td className="tabular text-right">{row.attempts}</Td>
                  <Td className="tabular text-xs text-fg-muted">
                    {formatTime(row.first_failed_at)}
                  </Td>
                  <Td className="tabular text-xs text-fg-muted">
                    {formatTime(row.last_failed_at)}
                  </Td>
                  <Td className="max-w-[26rem] truncate text-xs text-danger">{row.error}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}
