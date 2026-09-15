"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import {
  Badge, Button, Card, CardBody, CardHeader, EmptyState, ErrorNote,
  Field, Input, Select, Skeleton, Table, Td, Th,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { RejectionSummary, SimulationResult } from "@/lib/types";
import { formatTime, num, shortId } from "@/lib/utils";

export default function RiskPage() {
  return (
    <>
      <PageHeader
        title="Risk"
        description="Why copies were blocked, and what would happen if the master traded right now."
      />
      <Simulator />
      <Rejections />
    </>
  );
}

/**
 * Answering "what would every member get if I took 2 lots of GBPJPY right now"
 * BEFORE taking the trade is the cheapest safety feature in the product.
 */
function Simulator() {
  const [form, setForm] = React.useState({
    symbol: "EURUSD", side: "BUY", volume: "1.00",
    price: "1.17250", stop_loss: "1.17000", take_profit: "1.17750",
  });

  const simulate = useMutation({
    mutationFn: () =>
      api.post<SimulationResult>("/risk/simulate", {
        symbol: form.symbol,
        side: form.side,
        volume: Number(form.volume),
        price: Number(form.price),
        stop_loss: form.stop_loss ? Number(form.stop_loss) : null,
        take_profit: form.take_profit ? Number(form.take_profit) : null,
      }),
  });

  const set = (key: keyof typeof form) =>
    (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
      setForm((previous) => ({ ...previous, [key]: event.target.value }));

  return (
    <Card>
      <CardHeader
        title="Dry run"
        action={
          <span className="text-2xs text-fg-subtle">Nothing is written or executed</span>
        }
      />
      <CardBody className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Field label="Symbol"><Input value={form.symbol} onChange={set("symbol")} /></Field>
          <Field label="Side">
            <Select value={form.side} onChange={set("side")}>
              <option>BUY</option>
              <option>SELL</option>
            </Select>
          </Field>
          <Field label="Volume"><Input value={form.volume} onChange={set("volume")} /></Field>
          <Field label="Price"><Input value={form.price} onChange={set("price")} /></Field>
          <Field label="Stop loss"><Input value={form.stop_loss} onChange={set("stop_loss")} /></Field>
          <Field label="Take profit"><Input value={form.take_profit} onChange={set("take_profit")} /></Field>
        </div>

        <Button
          variant="primary"
          disabled={simulate.isPending}
          onClick={() => simulate.mutate()}
        >
          {simulate.isPending ? "Simulating…" : "Simulate"}
        </Button>

        {simulate.error ? <ErrorNote error={simulate.error} /> : null}

        {simulate.data ? (
          <div>
            <p className="mb-2 text-sm">
              <span className="font-medium">{simulate.data.summary.would_copy}</span> of{" "}
              {simulate.data.summary.total} members would receive this trade.
            </p>
            <Table>
              <thead>
                <tr>
                  <Th>Member</Th>
                  <Th>Mode</Th>
                  <Th>Symbol</Th>
                  <Th className="text-right">Calculated</Th>
                  <Th className="text-right">Final lot</Th>
                  <Th>Outcome</Th>
                  <Th>Reason</Th>
                </tr>
              </thead>
              <tbody>
                {simulate.data.members.map((member) => (
                  <tr key={member.member_account_id}>
                    <Td className="font-medium">{member.label}</Td>
                    <Td><Badge tone={member.mode === "LIVE" ? "good" : "info"}>{member.mode}</Badge></Td>
                    <Td>{member.symbol}</Td>
                    <Td className="tabular text-right text-fg-muted">
                      {num(member.calculated_lot)}
                    </Td>
                    <Td className="tabular text-right font-medium">{num(member.final_lot)}</Td>
                    <Td>
                      <Badge tone={member.would_copy ? "good" : "bad"}>
                        {member.would_copy ? "would copy" : "blocked"}
                      </Badge>
                    </Td>
                    <Td className="max-w-[20rem] truncate text-xs text-fg-muted">
                      {member.detail ?? member.reason ?? "—"}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}

function Rejections() {
  const [hours, setHours] = React.useState(24);
  const { data, isLoading, error } = useQuery({
    queryKey: ["rejections", hours],
    queryFn: () => api.get<RejectionSummary>(`/risk/rejections?hours=${hours}`),
  });

  return (
    <Card>
      <CardHeader
        title="Blocked copies"
        action={
          <Select
            className="h-7 w-32"
            value={hours}
            onChange={(event) => setHours(Number(event.target.value))}
          >
            <option value={1}>Last hour</option>
            <option value={24}>Last 24 hours</option>
            <option value={168}>Last 7 days</option>
          </Select>
        }
      />
      {error ? (
        <CardBody><ErrorNote error={error} /></CardBody>
      ) : isLoading ? (
        <CardBody><Skeleton className="h-32" /></CardBody>
      ) : !data || data.total === 0 ? (
        <EmptyState
          title="Nothing was blocked in this window"
          hint="A limit that rejects everything looks the same as a broken integration until you can see the reasons."
        />
      ) : (
        <>
          <CardBody className="flex flex-wrap gap-2">
            {data.by_reason.map((row) => (
              <Badge key={row.reason} tone="bad">
                {row.reason.replace(/_/g, " ")} · {row.count}
              </Badge>
            ))}
          </CardBody>
          <Table>
            <thead>
              <tr>
                <Th>Time</Th>
                <Th>Member</Th>
                <Th>Symbol</Th>
                <Th>Reason</Th>
                <Th>Detail</Th>
              </tr>
            </thead>
            <tbody>
              {data.recent.map((row) => (
                <tr key={row.id}>
                  <Td className="tabular text-xs text-fg-muted">{formatTime(row.created_at)}</Td>
                  <Td className="font-mono text-xs">{shortId(row.member_account_id)}</Td>
                  <Td>{row.symbol}</Td>
                  <Td className="text-xs">{row.reason?.replace(/_/g, " ")}</Td>
                  <Td className="text-xs text-fg-muted">{row.detail}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </>
      )}
    </Card>
  );
}
