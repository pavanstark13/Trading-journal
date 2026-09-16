"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Circle, Copy } from "lucide-react";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import { Dialog } from "@/components/ui/dialog";
import {
  Badge, Button, Card, CardBody, CardHeader, ConnectionDot, EmptyState,
  ErrorNote, Field, Input, Select, Skeleton,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { AccountRow } from "@/lib/types";
import { num, relativeTime } from "@/lib/utils";

export default function AccountsPage() {
  const queryClient = useQueryClient();
  const [adding, setAdding] = React.useState(false);
  const [code, setCode] = React.useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api.get<AccountRow[]>("/accounts"),
    refetchInterval: 15_000,
  });

  const reissue = useMutation({
    mutationFn: (id: string) =>
      api.post<{ install_code: string }>(`/accounts/${id}/install-code`),
    onSuccess: (result) => setCode(result.install_code),
  });

  const rebuild = useMutation({
    mutationFn: (id: string) => api.post<{ trades_built: number }>(`/accounts/${id}/rebuild`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["accounts"] });
      queryClient.invalidateQueries({ queryKey: ["trades"] });
      queryClient.invalidateQueries({ queryKey: ["overview"] });
    },
  });

  const accounts = (data ?? []).filter((a) => !a.is_archived);

  return (
    <>
      <PageHeader
        title="Accounts"
        description="Connect MetaTrader once. Your trades upload themselves from then on."
        action={<Button variant="primary" onClick={() => setAdding(true)}>Connect an account</Button>}
      />

      {error ? <ErrorNote error={error} /> : null}

      {isLoading ? (
        <Skeleton className="h-40" />
      ) : accounts.length === 0 ? (
        <Card>
          <EmptyState
            title="No accounts connected"
            hint="You will get a short code to paste into MetaTrader. Nothing else is needed — no password, no manual entry."
          />
        </Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {accounts.map((account) => (
            <Card key={account.id}>
              <CardHeader
                title={account.label}
                action={
                  <ConnectionDot
                    status={account.connected ? "ONLINE" : "OFFLINE"}
                    label={account.connected ? "syncing" : relativeTime(account.last_heartbeat_at)}
                  />
                }
              />
              <CardBody className="space-y-3 text-sm">
                {account.ea?.awaiting_setup ? (
                  <p className="rounded-md border border-accent/40 bg-accent/10 px-3 py-2 text-xs text-accent">
                    Waiting for MetaTrader. Install the file and paste your code — this
                    card turns green by itself within about 30 seconds.
                  </p>
                ) : null}

                <div className="grid grid-cols-2 gap-y-2">
                  <Detail label="Broker" value={account.broker_name ?? account.broker_server} />
                  <Detail label="Login" value={String(account.mt5_login || "—")} />
                  <Detail label="Balance" value={`${num(account.balance)} ${account.currency}`} />
                  <Detail label="Equity" value={num(account.equity)} />
                  <Detail label="Trades" value={String(account.trade_count)} />
                  <Detail
                    label="Broker records"
                    value={String(account.deal_count)}
                  />
                </div>

                {account.sync_error ? (
                  <p className="rounded bg-danger/10 px-2 py-1.5 text-xs text-danger">
                    {account.sync_error}
                  </p>
                ) : null}

                <div className="flex flex-wrap gap-2 pt-1">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => reissue.mutate(account.id)}
                  >
                    New install code
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={rebuild.isPending}
                    onClick={() => rebuild.mutate(account.id)}
                    title="Recalculate your trades from the broker's records. Your notes are never affected."
                  >
                    {rebuild.isPending ? "Recalculating…" : "Recalculate trades"}
                  </Button>
                </div>
              </CardBody>
            </Card>
          ))}
        </div>
      )}

      <AddAccountDialog
        open={adding}
        onClose={() => setAdding(false)}
        onCreated={(installCode) => {
          setAdding(false);
          setCode(installCode);
          queryClient.invalidateQueries({ queryKey: ["accounts"] });
        }}
      />

      <InstallDialog code={code} onClose={() => setCode(null)} />
    </>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-2xs uppercase tracking-wider text-fg-subtle">{label}</p>
      <p className="tabular">{value}</p>
    </div>
  );
}

function AddAccountDialog({
  open, onClose, onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (code: string) => void;
}) {
  const [label, setLabel] = React.useState("");
  const [currency, setCurrency] = React.useState("USD");
  const [startingBalance, setStartingBalance] = React.useState("");

  const create = useMutation({
    mutationFn: () =>
      api.post<{ id: string; install_code: string }>("/accounts", {
        label: label || "My account",
        currency,
        starting_balance: startingBalance || null,
      }),
    onSuccess: (result) => onCreated(result.install_code),
  });

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Connect a MetaTrader account"
      description="You will get a code to paste into MetaTrader. We never ask for your trading password."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={create.isPending} onClick={() => create.mutate()}>
            {create.isPending ? "Creating…" : "Get my code"}
          </Button>
        </>
      }
    >
      <Field label="What do you want to call it?">
        <Input
          value={label}
          onChange={(event) => setLabel(event.target.value)}
          placeholder="My live account"
        />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Account currency">
          <Select value={currency} onChange={(event) => setCurrency(event.target.value)}>
            {["USD", "EUR", "GBP", "INR", "AUD", "JPY"].map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </Select>
        </Field>
        <Field label="Starting balance" hint="Optional — used for the equity curve">
          <Input
            inputMode="decimal"
            value={startingBalance}
            onChange={(event) => setStartingBalance(event.target.value)}
            placeholder="10000"
          />
        </Field>
      </div>
      {create.error ? <ErrorNote error={create.error} /> : null}
    </Dialog>
  );
}

const STEPS = [
  {
    title: "Download the file",
    body: "Get JournalPublisher.mq5 from the setup guide and save it somewhere you can find.",
  },
  {
    title: "Open MetaTrader's data folder",
    body: "In MetaTrader: File → Open Data Folder. Put the file into the MQL5 → Experts folder.",
  },
  {
    title: "Allow it to reach this site",
    body: "Tools → Options → Expert Advisors. Tick 'Allow WebRequest for listed URL' and add this site's address. Most setup problems are this step.",
  },
  {
    title: "Drag it onto any chart",
    body: "The chart's symbol does not matter. Paste your code into the InpInstallCode box and press OK.",
  },
];

function InstallDialog({ code, onClose }: { code: string | null; onClose: () => void }) {
  const [copied, setCopied] = React.useState(false);

  return (
    <Dialog
      open={Boolean(code)}
      onClose={onClose}
      title="Your install code"
      description="Valid for 48 hours and usable once. You can always generate another."
    >
      <div className="flex items-center gap-2">
        <code className="flex-1 rounded bg-bg-sunken px-3 py-2.5 text-center font-mono text-lg tracking-widest">
          {code}
        </code>
        <Button
          size="icon"
          variant="secondary"
          aria-label="Copy code"
          onClick={() => {
            if (code) navigator.clipboard?.writeText(code);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          }}
        >
          {copied ? <CheckCircle2 className="h-4 w-4 text-long" /> : <Copy className="h-4 w-4" />}
        </Button>
      </div>

      <ol className="space-y-2.5 pt-1">
        {STEPS.map((step, index) => (
          <li key={step.title} className="flex gap-2.5">
            <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-bg-sunken text-2xs font-semibold">
              {index + 1}
            </span>
            <div>
              <p className="text-sm font-medium">{step.title}</p>
              <p className="text-xs text-fg-muted">{step.body}</p>
            </div>
          </li>
        ))}
      </ol>

      <p className="flex items-start gap-2 rounded-md border border-line px-3 py-2 text-xs text-fg-muted">
        <Circle className="mt-0.5 h-3 w-3 shrink-0" />
        The file only reads your history and uploads it. It never places, changes or
        closes a trade, and it never sends your password.
      </p>
    </Dialog>
  );
}
