"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import { Dialog } from "@/components/ui/dialog";
import {
  Badge, Button, Card, ConnectionDot, EmptyState, ErrorNote, Field,
  Input, Select, Skeleton, StatusBadge, Table, Td, Th,
} from "@/components/ui/primitives";
import { useAuth } from "@/hooks/useAuth";
import { api } from "@/lib/api";
import type { MemberRow } from "@/lib/types";
import { num, relativeTime } from "@/lib/utils";

export default function MembersPage() {
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const [creating, setCreating] = React.useState(false);
  const [installCode, setInstallCode] = React.useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["members"],
    queryFn: () => api.get<MemberRow[]>("/members"),
  });

  const issueCode = useMutation({
    mutationFn: (id: string) =>
      api.post<{ install_code: string }>(`/members/${id}/install-code`),
    onSuccess: (result) => setInstallCode(result.install_code),
  });

  const setActive = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      api.post(`/members/${id}/${active ? "activate" : "deactivate"}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["members"] }),
  });

  return (
    <>
      <PageHeader
        title="Members"
        description="Accounts receiving copied trades, and the state of their terminals."
        action={
          can("ADMIN") ? (
            <Button variant="primary" onClick={() => setCreating(true)}>
              Add member
            </Button>
          ) : null
        }
      />

      <Card>
        {error ? (
          <div className="p-4"><ErrorNote error={error} /></div>
        ) : isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 4 }).map((_, index) => (
              <Skeleton key={index} className="h-10" />
            ))}
          </div>
        ) : !data || data.length === 0 ? (
          <EmptyState
            title="No members yet"
            hint="Add a member, issue an install code, then have them attach MemberTradeCopier.mq5."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Member</Th>
                <Th>MT5 account</Th>
                <Th>Mode</Th>
                <Th>Copying</Th>
                <Th>EA</Th>
                <Th className="text-right">Balance</Th>
                <Th className="text-right">Equity</Th>
                <Th className="text-right">Open</Th>
                <Th>Status</Th>
                {can("ADMIN") ? <Th /> : null}
              </tr>
            </thead>
            <tbody>
              {data.map((member) => (
                <tr key={member.id} className="hover:bg-bg-sunken/60">
                  <Td>
                    <Link
                      href={`/members/${member.id}`}
                      className="font-medium hover:text-accent hover:underline"
                    >
                      {member.label}
                    </Link>
                    <p className="text-2xs text-fg-subtle">{member.email}</p>
                  </Td>
                  <Td className="tabular text-xs">
                    {member.mt5_login}
                    <span className="block text-2xs text-fg-subtle">
                      {member.broker_server}
                    </span>
                  </Td>
                  <Td>
                    <Badge tone={member.mode === "LIVE" ? "good" : "info"}>
                      {member.mode}
                    </Badge>
                  </Td>
                  <Td>
                    <Badge tone={member.copy_enabled ? "good" : "neutral"}>
                      {member.copy_enabled ? "on" : "off"}
                    </Badge>
                  </Td>
                  <Td>
                    <ConnectionDot
                      status={member.ea_online ? "ONLINE" : "OFFLINE"}
                      label={relativeTime(member.last_heartbeat_at)}
                    />
                  </Td>
                  <Td className="tabular text-right">{num(member.balance)}</Td>
                  <Td className="tabular text-right">{num(member.equity)}</Td>
                  <Td className="tabular text-right">{member.open_positions ?? 0}</Td>
                  <Td><StatusBadge status={member.status} /></Td>
                  {can("ADMIN") ? (
                    <Td>
                      <div className="flex justify-end gap-1.5">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => issueCode.mutate(member.id)}
                        >
                          Install code
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() =>
                            setActive.mutate({
                              id: member.id,
                              active: member.status !== "ACTIVE",
                            })
                          }
                        >
                          {member.status === "ACTIVE" ? "Suspend" : "Activate"}
                        </Button>
                      </div>
                    </Td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <CreateMemberDialog open={creating} onClose={() => setCreating(false)} />

      <Dialog
        open={Boolean(installCode)}
        onClose={() => setInstallCode(null)}
        title="EA install code"
        description="Single use, expires in 24 hours. Paste it into InpInstallCode in MemberTradeCopier."
      >
        <code className="block rounded bg-bg-sunken px-3 py-2 text-center font-mono text-lg tracking-widest">
          {installCode}
        </code>
        <p className="text-xs text-fg-muted">
          It is shown once. Generate a new one if it is lost — codes are cheap, and an
          old one stops working the moment it is used.
        </p>
      </Dialog>
    </>
  );
}

function CreateMemberDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = React.useState({
    email: "", full_name: "", label: "", mt5_login: "",
    broker_server: "", currency: "USD", mode: "PAPER", initial_password: "",
  });

  const create = useMutation({
    mutationFn: () =>
      api.post("/members", { ...form, mt5_login: Number(form.mt5_login) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["members"] });
      onClose();
    },
  });

  const set = (key: keyof typeof form) => (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((previous) => ({ ...previous, [key]: event.target.value }));

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Add member"
      description="Copying starts disabled for every new member. Enable it deliberately, once their terminal is connected and their risk limits are set."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            disabled={create.isPending}
            onClick={() => create.mutate()}
          >
            {create.isPending ? "Creating…" : "Create"}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Display name"><Input value={form.label} onChange={set("label")} /></Field>
        <Field label="Email"><Input type="email" value={form.email} onChange={set("email")} /></Field>
        <Field label="MT5 login">
          <Input inputMode="numeric" value={form.mt5_login} onChange={set("mt5_login")} />
        </Field>
        <Field label="Broker server">
          <Input value={form.broker_server} onChange={set("broker_server")} />
        </Field>
        <Field label="Currency">
          <Input maxLength={3} value={form.currency} onChange={set("currency")} />
        </Field>
        <Field label="Mode">
          <Select value={form.mode} onChange={set("mode")}>
            <option value="LIVE">LIVE</option>
            <option value="PAPER">PAPER (simulated fills)</option>
          </Select>
        </Field>
      </div>
      <Field
        label="Initial password"
        hint="At least 12 characters. The member should change it on first sign-in."
      >
        <Input
          type="password"
          value={form.initial_password}
          onChange={set("initial_password")}
        />
      </Field>
      <p className="text-2xs text-fg-subtle">
        No MT5 password is asked for, here or anywhere. The EA authenticates itself; your
        broker credentials never reach this system.
      </p>
      {create.error ? <ErrorNote error={create.error} /> : null}
    </Dialog>
  );
}
