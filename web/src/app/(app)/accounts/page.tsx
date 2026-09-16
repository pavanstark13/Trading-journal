"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Circle, Copy, Lock } from "lucide-react";
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

/**
 * Two ways in, and the right one depends on what the trader owns.
 *
 * MetaTrader on iPhone and iPad cannot run an Expert Advisor at all, and plenty of
 * traders have no Windows machine they can leave switched on. For them the terminal
 * is hosted and we read it with a read-only investor password. Anyone who does have
 * a PC running all day is better off with the file: nothing leaves their machine.
 */
type Method = "cloud" | "ea";

export default function AccountsPage() {
  const queryClient = useQueryClient();
  const [adding, setAdding] = React.useState(false);
  const [code, setCode] = React.useState<string | null>(null);
  const [connecting, setConnecting] = React.useState<AccountRow | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api.get<AccountRow[]>("/accounts"),
    refetchInterval: 15_000,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["accounts"] });
    queryClient.invalidateQueries({ queryKey: ["trades"] });
    queryClient.invalidateQueries({ queryKey: ["overview"] });
  };

  const reissue = useMutation({
    mutationFn: (id: string) =>
      api.post<{ install_code: string }>(`/accounts/${id}/install-code`),
    onSuccess: (result) => setCode(result.install_code),
  });

  const rebuild = useMutation({
    mutationFn: (id: string) => api.post<{ trades_built: number }>(`/accounts/${id}/rebuild`),
    onSuccess: invalidate,
  });

  const syncNow = useMutation({
    mutationFn: (id: string) => api.post<{ deals_new: number }>(`/accounts/${id}/sync`),
    onSuccess: invalidate,
  });

  const disconnect = useMutation({
    mutationFn: (id: string) => api.delete<AccountRow>(`/accounts/${id}/connect`),
    onSuccess: invalidate,
  });

  const accounts = (data ?? []).filter((a) => !a.is_archived);

  return (
    <>
      <PageHeader
        title="Accounts"
        description="Connect MetaTrader once. Your trades import themselves from then on."
        action={<Button variant="primary" onClick={() => setAdding(true)}>Connect an account</Button>}
      />

      {error ? <ErrorNote error={error} /> : null}

      {isLoading ? (
        <Skeleton className="h-40" />
      ) : accounts.length === 0 ? (
        <Card>
          <EmptyState
            title="No accounts connected"
            hint="Takes a minute. You will never be asked for a password that can place a trade."
          />
        </Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {accounts.map((account) => {
            const cloud = account.sync_source === "cloud";
            const pending = cloud && !account.provider;
            return (
              <Card key={account.id}>
                <CardHeader
                  title={account.label}
                  action={
                    <ConnectionDot
                      status={account.connected ? "ONLINE" : "OFFLINE"}
                      label={
                        account.connected
                          ? cloud ? "reading" : "syncing"
                          : relativeTime(
                              cloud ? account.provider_synced_at : account.last_heartbeat_at,
                            )
                      }
                    />
                  }
                />
                <CardBody className="space-y-3 text-sm">
                  {pending ? (
                    <p className="rounded-md border border-accent/40 bg-accent/10 px-3 py-2 text-xs text-accent">
                      Not connected yet. Add your login details to start the import.
                    </p>
                  ) : null}

                  {account.ea?.awaiting_setup && !cloud ? (
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
                    <Detail label="Broker records" value={String(account.deal_count)} />
                  </div>

                  <Badge tone="neutral">
                    {cloud ? "Read for you — read-only access" : "MetaTrader on your PC"}
                  </Badge>

                  {account.sync_error ? (
                    <p className="rounded bg-danger/10 px-2 py-1.5 text-xs text-danger">
                      {account.sync_error}
                    </p>
                  ) : null}

                  <div className="flex flex-wrap gap-2 pt-1">
                    {cloud ? (
                      <>
                        <Button
                          size="sm"
                          variant={pending ? "primary" : "secondary"}
                          onClick={() => setConnecting(account)}
                        >
                          {pending ? "Add login details" : "Change login details"}
                        </Button>
                        {!pending ? (
                          <Button
                            size="sm"
                            variant="secondary"
                            disabled={syncNow.isPending}
                            onClick={() => syncNow.mutate(account.id)}
                          >
                            {syncNow.isPending ? "Checking…" : "Check for new trades"}
                          </Button>
                        ) : null}
                        {!pending ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={disconnect.isPending}
                            onClick={() => disconnect.mutate(account.id)}
                            title="Stops reading the account. Your imported history and notes stay."
                          >
                            Disconnect
                          </Button>
                        ) : null}
                      </>
                    ) : (
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => reissue.mutate(account.id)}
                      >
                        New install code
                      </Button>
                    )}
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

                  {syncNow.error ? <ErrorNote error={syncNow.error} /> : null}
                  {disconnect.error ? <ErrorNote error={disconnect.error} /> : null}
                </CardBody>
              </Card>
            );
          })}
        </div>
      )}

      <AddAccountDialog
        open={adding}
        onClose={() => setAdding(false)}
        onCreated={(account, method) => {
          setAdding(false);
          queryClient.invalidateQueries({ queryKey: ["accounts"] });
          if (method === "cloud") setConnecting(account);
          else setCode(account.install_code ?? null);
        }}
      />

      <ConnectDialog
        account={connecting}
        onClose={() => setConnecting(null)}
        onConnected={() => {
          setConnecting(null);
          invalidate();
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

/* ── choosing how to connect ───────────────────────────────────────────────── */

function MethodChoice({
  value, onChange,
}: {
  value: Method;
  onChange: (next: Method) => void;
}) {
  const options: { id: Method; title: string; body: string }[] = [
    {
      id: "cloud",
      title: "Read it for me",
      body:
        "Works from an iPad, a phone, a Mac — anything. You give a read-only password " +
        "that cannot place or close a trade, and nothing has to stay switched on.",
    },
    {
      id: "ea",
      title: "I'll install a file in MetaTrader",
      body:
        "Windows only, and the PC has to be running. Nothing leaves your machine except " +
        "your finished trades.",
    },
  ];

  return (
    <div className="space-y-2">
      {options.map((option) => (
        <button
          key={option.id}
          type="button"
          onClick={() => onChange(option.id)}
          aria-pressed={value === option.id}
          className={
            "block w-full rounded-md border px-3 py-2.5 text-left transition-colors " +
            (value === option.id
              ? "border-accent bg-accent/10"
              : "border-line hover:border-fg-subtle")
          }
        >
          <span className="flex items-center gap-2 text-sm font-medium">
            {value === option.id ? (
              <CheckCircle2 className="h-4 w-4 text-accent" />
            ) : (
              <Circle className="h-4 w-4 text-fg-subtle" />
            )}
            {option.title}
          </span>
          <span className="mt-1 block pl-6 text-xs text-fg-muted">{option.body}</span>
        </button>
      ))}
    </div>
  );
}

type CreatedAccount = AccountRow & { install_code?: string };

function AddAccountDialog({
  open, onClose, onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (account: CreatedAccount, method: Method) => void;
}) {
  const [label, setLabel] = React.useState("");
  const [currency, setCurrency] = React.useState("USD");
  const [startingBalance, setStartingBalance] = React.useState("");
  const [method, setMethod] = React.useState<Method>("cloud");

  const create = useMutation({
    mutationFn: async () => {
      const created = await api.post<{ id: string; install_code: string | null }>(
        "/accounts",
        {
          label: label || "My account",
          currency,
          starting_balance: startingBalance || null,
          sync_source: method,
        },
      );
      const account = await api.get<AccountRow>(`/accounts/${created.id}`);
      return { ...account, install_code: created.install_code ?? undefined } as CreatedAccount;
    },
    onSuccess: (account) => onCreated(account, method),
  });

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Connect a MetaTrader account"
      description="We never ask for a password that can trade on your account."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={create.isPending} onClick={() => create.mutate()}>
            {create.isPending ? "Creating…" : "Continue"}
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

      <div className="space-y-1.5">
        <p className="text-xs font-medium text-fg-muted">How should we get your trades?</p>
        <MethodChoice value={method} onChange={setMethod} />
      </div>

      {create.error ? <ErrorNote error={create.error} /> : null}
    </Dialog>
  );
}

/* ── the cloud path ────────────────────────────────────────────────────────── */

function ConnectDialog({
  account, onClose, onConnected,
}: {
  account: AccountRow | null;
  onClose: () => void;
  onConnected: () => void;
}) {
  const [login, setLogin] = React.useState("");
  const [server, setServer] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [marginMode, setMarginMode] = React.useState<"hedging" | "netting">("hedging");

  React.useEffect(() => {
    if (!account) return;
    setLogin(account.mt5_login ? String(account.mt5_login) : "");
    setServer(account.broker_server === "pending" ? "" : account.broker_server);
    setPassword("");
    setMarginMode(account.margin_mode);
  }, [account]);

  const connect = useMutation({
    mutationFn: () =>
      api.post<AccountRow>(`/accounts/${account!.id}/connect`, {
        mt5_login: Number(login),
        broker_server: server.trim(),
        investor_password: password,
        margin_mode: marginMode,
      }),
    onSuccess: async () => {
      // Kick the first import straight away rather than making them wait for the
      // next poll. Failures here are shown on the card, not as a dead end.
      try {
        await api.post(`/accounts/${account!.id}/sync?full=true`);
      } catch {
        /* the poller will retry */
      }
      setPassword("");
      onConnected();
    },
  });

  const ready = login.trim() !== "" && server.trim() !== "" && password !== "";

  return (
    <Dialog
      open={Boolean(account)}
      onClose={onClose}
      title="Your MetaTrader login details"
      description="Use your INVESTOR password — the read-only one. It cannot open, change or close a trade."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            disabled={!ready || connect.isPending}
            onClick={() => connect.mutate()}
          >
            {connect.isPending ? "Connecting…" : "Connect and import"}
          </Button>
        </>
      }
    >
      <Field label="Login number" hint="The account number your broker gave you">
        <Input
          inputMode="numeric"
          value={login}
          onChange={(event) => setLogin(event.target.value.replace(/\D/g, ""))}
          placeholder="1234567"
        />
      </Field>

      <Field
        label="Server name"
        hint="Copy it exactly as MetaTrader shows it, e.g. MultiBank-Live. A single wrong character fails."
      >
        <Input
          value={server}
          onChange={(event) => setServer(event.target.value)}
          placeholder="MultiBank-Live"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
        />
      </Field>

      <Field
        label="Investor password"
        hint="Not your normal password. In MetaTrader: your account → Change password → Investor."
      >
        <Input
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoComplete="off"
        />
      </Field>

      <Field
        label="Account type"
        hint="If you can hold a buy and a sell on the same pair at once, it is hedging."
      >
        <Select
          value={marginMode}
          onChange={(event) => setMarginMode(event.target.value as "hedging" | "netting")}
        >
          <option value="hedging">Hedging (most forex brokers)</option>
          <option value="netting">Netting</option>
        </Select>
      </Field>

      <p className="flex items-start gap-2 rounded-md border border-line px-3 py-2 text-xs text-fg-muted">
        <Lock className="mt-0.5 h-3 w-3 shrink-0" />
        The investor password is read-only: your broker&apos;s own server refuses every
        trading action made with it. We pass it straight to the service that reads your
        history and never store it here.
      </p>

      {connect.error ? <ErrorNote error={connect.error} /> : null}
    </Dialog>
  );
}

/* ── the Expert Advisor path ───────────────────────────────────────────────── */

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
