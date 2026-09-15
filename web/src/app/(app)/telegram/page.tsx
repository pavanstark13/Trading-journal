"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import { Dialog } from "@/components/ui/dialog";
import {
  Badge, Button, Card, CardBody, CardHeader, EmptyState, ErrorNote,
  Field, Input, Skeleton, StatusBadge, Table, Td, Th, Toggle,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { TelegramChannelRow } from "@/lib/types";
import { formatTime, relativeTime } from "@/lib/utils";

const EVENT_TYPES = [
  "TRADE_OPENED", "TRADE_MODIFIED", "TRADE_CLOSED", "TRADE_PARTIAL_CLOSED",
  "PENDING_ORDER_CREATED", "PENDING_ORDER_CANCELLED", "PENDING_ORDER_TRIGGERED",
];

export default function TelegramPage() {
  const queryClient = useQueryClient();
  const [creating, setCreating] = React.useState(false);
  const [editing, setEditing] = React.useState<TelegramChannelRow | null>(null);

  const channels = useQuery({
    queryKey: ["telegram-channels"],
    queryFn: () => api.get<TelegramChannelRow[]>("/telegram/channels"),
  });

  const test = useMutation({
    mutationFn: (id: string) =>
      api.post<{ ok: boolean; bot_username: string }>(`/telegram/channels/${id}/test`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["telegram-channels"] }),
  });

  const patch = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      api.patch(`/telegram/channels/${id}`, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["telegram-channels"] }),
  });

  return (
    <>
      <PageHeader
        title="Telegram"
        description="Channels the master's trades are published to, and the delivery queue."
        action={<Button variant="primary" onClick={() => setCreating(true)}>Add channel</Button>}
      />

      {test.data ? (
        <Card className="border-long/40 bg-long/5">
          <CardBody className="text-sm text-long">
            Test message delivered by @{test.data.bot_username}. The channel is configured
            correctly.
          </CardBody>
        </Card>
      ) : null}
      {test.error ? <ErrorNote error={test.error} /> : null}

      {channels.isLoading ? (
        <Skeleton className="h-40" />
      ) : channels.error ? (
        <ErrorNote error={channels.error} />
      ) : !channels.data || channels.data.length === 0 ? (
        <Card>
          <EmptyState
            title="No channel configured"
            hint="Create a bot with @BotFather, add it to your channel as an administrator, then add it here."
          />
        </Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {channels.data.map((channel) => (
            <Card key={channel.id}>
              <CardHeader
                title={channel.label}
                action={
                  <Toggle
                    checked={channel.is_enabled}
                    onChange={(next) =>
                      patch.mutate({ id: channel.id, body: { is_enabled: next } })
                    }
                  />
                }
              />
              <CardBody className="space-y-3 text-sm">
                <div className="flex items-center justify-between">
                  <span className="text-fg-muted">Chat</span>
                  <code className="font-mono text-xs">{channel.chat_id}</code>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-fg-muted">Last delivery</span>
                  <span>{relativeTime(channel.last_ok_at)}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-fg-muted">Edit cards in place</span>
                  <Toggle
                    checked={channel.edit_in_place}
                    onChange={(next) =>
                      patch.mutate({ id: channel.id, body: { edit_in_place: next } })
                    }
                  />
                </div>
                <div className="flex flex-wrap gap-1">
                  {channel.publish_types.map((type) => (
                    <Badge key={type} tone="info">{type.replace(/_/g, " ")}</Badge>
                  ))}
                </div>
                {channel.last_error ? (
                  <p className="rounded bg-danger/10 px-2 py-1.5 text-xs text-danger">
                    {channel.last_error}
                  </p>
                ) : null}
                <div className="flex gap-2 pt-1">
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={test.isPending}
                    onClick={() => test.mutate(channel.id)}
                  >
                    Send test
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setEditing(channel)}>
                    Edit
                  </Button>
                </div>
              </CardBody>
            </Card>
          ))}
        </div>
      )}

      <MessageQueue />

      <ChannelDialog
        open={creating}
        onClose={() => setCreating(false)}
        onSaved={() => queryClient.invalidateQueries({ queryKey: ["telegram-channels"] })}
      />
      <ChannelDialog
        open={Boolean(editing)}
        channel={editing}
        onClose={() => setEditing(null)}
        onSaved={() => queryClient.invalidateQueries({ queryKey: ["telegram-channels"] })}
      />
    </>
  );
}

function MessageQueue() {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ["telegram-messages"],
    queryFn: () =>
      api.get<
        {
          id: string;
          status: string;
          attempts: number;
          error: string | null;
          sent_at: string | null;
          next_attempt_at: string | null;
        }[]
      >("/telegram/messages?limit=50"),
    refetchInterval: 20_000,
  });

  const retry = useMutation({
    mutationFn: (id: string) => api.post(`/telegram/messages/${id}/retry`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["telegram-messages"] }),
  });

  return (
    <Card>
      <CardHeader title="Delivery queue" />
      {!data || data.length === 0 ? (
        <EmptyState title="Nothing queued" />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Status</Th>
              <Th className="text-right">Attempts</Th>
              <Th>Sent</Th>
              <Th>Next attempt</Th>
              <Th>Error</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {data.map((message) => (
              <tr key={message.id}>
                <Td><StatusBadge status={message.status} /></Td>
                <Td className="tabular text-right">{message.attempts}</Td>
                <Td className="tabular text-xs text-fg-muted">{formatTime(message.sent_at)}</Td>
                <Td className="tabular text-xs text-fg-muted">
                  {formatTime(message.next_attempt_at)}
                </Td>
                <Td className="max-w-[22rem] truncate text-xs text-danger">
                  {message.error ?? "—"}
                </Td>
                <Td>
                  {message.status === "DEAD" || message.status === "FAILED" ? (
                    <Button size="sm" variant="ghost" onClick={() => retry.mutate(message.id)}>
                      Retry
                    </Button>
                  ) : null}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

function ChannelDialog({
  open,
  channel,
  onClose,
  onSaved,
}: {
  open: boolean;
  channel?: TelegramChannelRow | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [label, setLabel] = React.useState("");
  const [chatId, setChatId] = React.useState("");
  const [botToken, setBotToken] = React.useState("");
  const [types, setTypes] = React.useState<string[]>([
    "TRADE_OPENED", "TRADE_MODIFIED", "TRADE_CLOSED",
  ]);

  React.useEffect(() => {
    if (!open) return;
    setLabel(channel?.label ?? "");
    setChatId(channel?.chat_id ?? "");
    setBotToken("");
    setTypes(channel?.publish_types ?? ["TRADE_OPENED", "TRADE_MODIFIED", "TRADE_CLOSED"]);
  }, [open, channel]);

  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        label, chat_id: chatId, publish_types: types,
      };
      if (botToken) body.bot_token = botToken;
      return channel
        ? api.patch(`/telegram/channels/${channel.id}`, body)
        : api.post("/telegram/channels", { ...body, bot_token: botToken });
    },
    onSuccess: () => {
      onSaved();
      onClose();
    },
  });

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={channel ? "Edit channel" : "Add channel"}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <Field label="Label"><Input value={label} onChange={(e) => setLabel(e.target.value)} /></Field>
      <Field
        label="Chat ID"
        hint="@channelname for a public channel, or the numeric -100… id for a private one."
      >
        <Input value={chatId} onChange={(e) => setChatId(e.target.value)} />
      </Field>
      <Field
        label={channel ? "Bot token (leave blank to keep)" : "Bot token"}
        hint="From @BotFather. Stored encrypted; never shown again and never logged."
      >
        <Input
          type="password"
          value={botToken}
          onChange={(e) => setBotToken(e.target.value)}
          placeholder={channel?.has_token ? "••••••••" : ""}
        />
      </Field>
      <div>
        <p className="mb-1.5 text-xs font-medium text-fg-muted">Publish these events</p>
        <div className="flex flex-wrap gap-1.5">
          {EVENT_TYPES.map((type) => {
            const active = types.includes(type);
            return (
              <button
                key={type}
                type="button"
                onClick={() =>
                  setTypes((previous) =>
                    active ? previous.filter((t) => t !== type) : [...previous, type],
                  )
                }
              >
                <Badge tone={active ? "info" : "neutral"}>{type.replace(/_/g, " ")}</Badge>
              </button>
            );
          })}
        </div>
      </div>
      {save.error ? <ErrorNote error={save.error} /> : null}
    </Dialog>
  );
}
