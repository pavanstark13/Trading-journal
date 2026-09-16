"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import { ConfirmPhraseDialog } from "@/components/ui/dialog";
import {
  Badge, Button, Card, CardBody, CardHeader, ErrorNote, Skeleton,
} from "@/components/ui/primitives";
import { useAuth } from "@/hooks/useAuth";
import { api } from "@/lib/api";
import { formatTime } from "@/lib/utils";

interface SystemSettings {
  mode: "PAPER" | "LIVE";
  copying_paused: boolean;
  emergency_stop: boolean;
  emergency_halts_telegram: boolean;
  live_activated_at: string | null;
}

export default function SettingsPage() {
  const { can, user } = useAuth();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = React.useState<null | "LIVE" | "PAPER">(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<SystemSettings>("/admin/settings"),
  });

  const setMode = useMutation({
    mutationFn: (mode: "PAPER" | "LIVE") =>
      api.post("/admin/mode", {
        mode,
        confirm: mode === "LIVE" ? "GO LIVE" : "BACK TO PAPER",
      }),
    onSuccess: () => {
      setConfirming(null);
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  if (error) return <ErrorNote error={error} />;
  if (isLoading || !data) return <Skeleton className="h-64" />;

  return (
    <>
      <PageHeader title="Settings" description="System-wide operating mode and safeguards." />

      <Card>
        <CardHeader
          title="Operating mode"
          action={
            <Badge tone={data.mode === "LIVE" ? "good" : "info"}>{data.mode}</Badge>
          }
        />
        <CardBody className="space-y-4">
          <p className="text-sm text-fg-muted">
            In <strong>LIVE</strong> mode copy orders reach real member terminals and
            real brokers. In <strong>PAPER</strong> they are planned, risk-checked and
            recorded identically, then filled by the simulator — useful for onboarding
            one member without holding the others back.
          </p>
          <p className="text-sm text-fg-muted">
            The mode is not what gates live trading. <strong>Copying is off for every
            member until you enable it individually</strong>, so a LIVE system with
            nobody enabled sends nothing.
          </p>

          {data.live_activated_at ? (
            <p className="text-xs text-fg-subtle">
              LIVE activated {formatTime(data.live_activated_at)}.
            </p>
          ) : null}

          {can("SUPER_ADMIN") ? (
            <div className="flex gap-2">
              {data.mode === "PAPER" ? (
                <Button variant="danger" onClick={() => setConfirming("LIVE")}>
                  Switch to LIVE
                </Button>
              ) : (
                <Button variant="secondary" onClick={() => setConfirming("PAPER")}>
                  Return to PAPER
                </Button>
              )}
            </div>
          ) : (
            <p className="text-xs text-fg-subtle">
              Only a SUPER_ADMIN can change the operating mode. You are signed in as{" "}
              {user?.role.replace("_", " ")}.
            </p>
          )}

          {setMode.error ? <ErrorNote error={setMode.error} /> : null}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Current safeguards" />
        <CardBody className="grid gap-3 sm:grid-cols-3 text-sm">
          <Safeguard label="Copying paused" active={data.copying_paused} />
          <Safeguard label="Emergency stop" active={data.emergency_stop} />
          <Safeguard
            label="Emergency also halts Telegram"
            active={data.emergency_halts_telegram}
          />
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Before you enable the first member" />
        <CardBody>
          <ol className="list-decimal space-y-1.5 pl-5 text-sm text-fg-muted">
            <li>Master EA shows ONLINE and a real trade has appeared on the Trades page.</li>
            <li>
              Every member terminal shows ONLINE, and their contract specifications have
              arrived — the Risk dry run says <code>broker</code>, not{" "}
              <code>fallback</code>, as the spec source.
            </li>
            <li>Risk limits are set for every member, especially max lot and daily loss.</li>
            <li>The Telegram channel has received a test message.</li>
            <li>A dry run on the Risk page produces exactly the lot sizes you expect.</li>
            <li>You know where the emergency stop is, and who can clear it.</li>
            <li>
              Enable members <strong>one at a time, smallest account first</strong>, and
              check Copy Orders after each live trade before enabling the next.
            </li>
          </ol>
        </CardBody>
      </Card>

      <ConfirmPhraseDialog
        open={confirming === "LIVE"}
        onClose={() => setConfirming(null)}
        onConfirm={() => setMode.mutate("LIVE")}
        title="Switch to LIVE trading"
        description="Copy orders will be sent to real member terminals and placed with their brokers. Every admin is notified and the change is audited."
        phrase="GO LIVE"
        confirmLabel="Go live"
        pending={setMode.isPending}
      />
      <ConfirmPhraseDialog
        open={confirming === "PAPER"}
        onClose={() => setConfirming(null)}
        onConfirm={() => setMode.mutate("PAPER")}
        title="Return to PAPER mode"
        description="New copy orders will be simulated instead of executed. Positions already open at members' brokers are not affected."
        phrase="BACK TO PAPER"
        confirmLabel="Return to paper"
        tone="warn"
        pending={setMode.isPending}
      />
    </>
  );
}

function Safeguard({ label, active }: { label: string; active: boolean }) {
  return (
    <div className="flex items-center justify-between rounded-md border border-line px-3 py-2">
      <span className="text-fg-muted">{label}</span>
      <Badge tone={active ? "warn" : "neutral"}>{active ? "active" : "off"}</Badge>
    </div>
  );
}
