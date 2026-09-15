"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import {
  Button, Card, CardBody, CardHeader, ErrorNote, Field, Input,
  Select, Skeleton, StatusBadge, Table, Td, Th, Toggle,
} from "@/components/ui/primitives";
import { useAuth } from "@/hooks/useAuth";
import { api } from "@/lib/api";
import type { CopyOrderRow, MemberRow } from "@/lib/types";
import { formatTime, num } from "@/lib/utils";

const SIZING_MODES = [
  ["BALANCE_PROPORTIONAL", "Balance proportional"],
  ["EQUITY_PROPORTIONAL", "Equity proportional"],
  ["LOT_MULTIPLIER", "Lot multiplier"],
  ["FIXED_LOT", "Fixed lot"],
  ["RISK_PERCENT", "Risk percent"],
  ["FIXED_MONEY_RISK", "Fixed money risk"],
] as const;

export default function MemberDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const { can } = useAuth();
  const queryClient = useQueryClient();

  const member = useQuery({
    queryKey: ["member", id],
    queryFn: () => api.get<MemberRow>(`/members/${id}`),
  });
  const copySettings = useQuery({
    queryKey: ["copy-settings", id],
    queryFn: () => api.get<Record<string, unknown>>(`/members/${id}/copy-settings`),
  });
  const riskSettings = useQuery({
    queryKey: ["risk-settings", id],
    queryFn: () => api.get<Record<string, unknown>>(`/members/${id}/risk-settings`),
  });
  const orders = useQuery({
    queryKey: ["member-copy-orders", id],
    queryFn: () => api.get<CopyOrderRow[]>(`/members/${id}/copy-orders?limit=25`),
  });

  const saveCopy = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.put(`/members/${id}/copy-settings`, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["copy-settings", id] }),
  });
  const saveRisk = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.put(`/members/${id}/risk-settings`, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["risk-settings", id] }),
  });

  if (member.isLoading) return <Skeleton className="h-64" />;
  if (member.error) return <ErrorNote error={member.error} />;
  if (!member.data) return null;

  const copy = copySettings.data ?? {};
  const risk = riskSettings.data ?? {};

  return (
    <>
      <PageHeader
        title={member.data.label}
        description={`${member.data.mt5_login} @ ${member.data.broker_server} · ${member.data.mode}`}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Copy settings"
            action={<span className="text-2xs text-fg-subtle">Editable by the member</span>}
          />
          <CardBody className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm">Copying enabled</span>
              <Toggle
                checked={Boolean(copy.copy_enabled)}
                onChange={(next) => saveCopy.mutate({ copy_enabled: next })}
              />
            </div>

            <Field label="Sizing mode">
              <Select
                value={String(copy.sizing_mode ?? "BALANCE_PROPORTIONAL")}
                onChange={(event) => saveCopy.mutate({ sizing_mode: event.target.value })}
              >
                {SIZING_MODES.map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </Select>
            </Field>

            <div className="grid grid-cols-2 gap-3">
              <NumberField
                label="Multiplier"
                value={copy.copy_multiplier}
                onSave={(value) => saveCopy.mutate({ copy_multiplier: value })}
              />
              <NumberField
                label="Fixed lot"
                value={copy.fixed_lot}
                onSave={(value) => saveCopy.mutate({ fixed_lot: value })}
              />
              <NumberField
                label="Risk %"
                value={copy.risk_percent}
                onSave={(value) => saveCopy.mutate({ risk_percent: value })}
              />
              <NumberField
                label="Fixed money risk"
                value={copy.fixed_money_risk}
                onSave={(value) => saveCopy.mutate({ fixed_money_risk: value })}
              />
            </div>

            <NumberField
              label="Max signal age (seconds)"
              hint="Older instructions are rejected rather than executed at a moved price."
              value={copy.max_signal_age_sec}
              onSave={(value) => saveCopy.mutate({ max_signal_age_sec: value })}
            />

            {(["copy_sl", "copy_tp", "copy_modifications", "copy_pending", "reverse_trades"] as const).map(
              (key) => (
                <div key={key} className="flex items-center justify-between">
                  <span className="text-sm">{key.replace(/_/g, " ")}</span>
                  <Toggle
                    checked={Boolean(copy[key])}
                    onChange={(next) => saveCopy.mutate({ [key]: next })}
                  />
                </div>
              ),
            )}
            {saveCopy.error ? <ErrorNote error={saveCopy.error} /> : null}
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="Risk limits"
            action={
              <span className="text-2xs text-fg-subtle">
                {can("ADMIN") ? "Admin only" : "Read only"}
              </span>
            }
          />
          <CardBody className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              {([
                ["max_lot", "Max lot"],
                ["min_lot", "Min lot"],
                ["max_trade_risk", "Max trade risk"],
                ["max_daily_loss", "Max daily loss"],
                ["max_daily_loss_pct", "Max daily loss %"],
                ["max_simultaneous_trades", "Max open trades"],
                ["max_daily_trades", "Max trades/day"],
                ["max_spread_points", "Max spread (points)"],
                ["max_slippage_points", "Max slippage (points)"],
              ] as const).map(([key, label]) => (
                <NumberField
                  key={key}
                  label={label}
                  value={risk[key]}
                  disabled={!can("ADMIN")}
                  onSave={(value) => saveRisk.mutate({ [key]: value })}
                />
              ))}
            </div>
            <p className="text-2xs text-fg-subtle">
              A member can never widen their own limits: risk settings are admin-write,
              member-read. That is the difference between a risk system and a suggestion.
            </p>
            {saveRisk.error ? <ErrorNote error={saveRisk.error} /> : null}
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardHeader title="Recent copy orders" />
        {orders.data && orders.data.length > 0 ? (
          <Table>
            <thead>
              <tr>
                <Th>Created</Th>
                <Th>Action</Th>
                <Th>Symbol</Th>
                <Th className="text-right">Lot</Th>
                <Th>Status</Th>
                <Th>Reason</Th>
              </tr>
            </thead>
            <tbody>
              {orders.data.map((order) => (
                <tr key={order.id}>
                  <Td className="tabular text-xs text-fg-muted">
                    {formatTime(order.created_at)}
                  </Td>
                  <Td className="text-xs">{order.action}</Td>
                  <Td>{order.symbol}</Td>
                  <Td className="tabular text-right">{num(order.final_lot)}</Td>
                  <Td><StatusBadge status={order.status} /></Td>
                  <Td className="text-xs text-fg-muted">{order.reject_reason ?? "—"}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <CardBody className="text-sm text-fg-muted">No copy orders yet.</CardBody>
        )}
      </Card>
    </>
  );
}

function NumberField({
  label,
  hint,
  value,
  onSave,
  disabled,
}: {
  label: string;
  hint?: string;
  value: unknown;
  onSave: (value: number | null) => void;
  disabled?: boolean;
}) {
  const [draft, setDraft] = React.useState(value === null || value === undefined ? "" : String(value));
  React.useEffect(() => {
    setDraft(value === null || value === undefined ? "" : String(value));
  }, [value]);

  return (
    <Field label={label} hint={hint}>
      <div className="flex gap-1.5">
        <Input
          inputMode="decimal"
          disabled={disabled}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="unset"
        />
        <Button
          size="sm"
          variant="secondary"
          disabled={disabled || draft === (value === null || value === undefined ? "" : String(value))}
          onClick={() => onSave(draft === "" ? null : Number(draft))}
        >
          Save
        </Button>
      </div>
    </Field>
  );
}
