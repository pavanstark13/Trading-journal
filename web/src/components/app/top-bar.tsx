"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LogOut, PauseCircle, PlayCircle, OctagonX } from "lucide-react";
import * as React from "react";

import { Badge, Button, ConnectionDot } from "@/components/ui/primitives";
import { ConfirmPhraseDialog } from "@/components/ui/dialog";
import { useAuth } from "@/hooks/useAuth";
import { useLiveFeed } from "@/hooks/useLiveFeed";
import { api } from "@/lib/api";
import type { DashboardData } from "@/lib/types";

/**
 * The bar answers the two questions an operator has on walking up to the screen:
 * is this live money, and is anything stopped?
 */
export function TopBar() {
  const { user, logout, can } = useAuth();
  const { connected } = useLiveFeed();
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = React.useState<null | "stop" | "pause" | "resume">(null);

  const { data } = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api.get<DashboardData>("/admin/dashboard"),
    enabled: can("ADMIN"),
    refetchInterval: 30_000,
  });

  const mutate = useMutation({
    mutationFn: async (action: "stop" | "pause" | "resume") => {
      if (action === "stop") {
        return api.post("/copy/emergency-stop", { confirm: "EMERGENCY STOP" });
      }
      return api.post(action === "pause" ? "/copy/pause" : "/copy/resume");
    },
    onSuccess: () => {
      setConfirm(null);
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
  });

  const system = data?.system;
  const paused = system?.copying_paused ?? false;
  const stopped = system?.emergency_stop ?? false;

  return (
    <>
      {stopped ? (
        <div className="flex items-center justify-center gap-2 bg-danger px-4 py-1.5 text-xs font-semibold text-white">
          <OctagonX className="h-3.5 w-3.5" />
          EMERGENCY STOP ACTIVE — all copy activity halted. Open positions are untouched.
        </div>
      ) : null}

      <header className="flex h-14 shrink-0 items-center gap-3 border-b border-line bg-bg-raised px-4">
        {system ? (
          <Badge tone={system.mode === "LIVE" ? "good" : "info"}>
            {system.mode === "LIVE" ? "LIVE TRADING" : "PAPER MODE"}
          </Badge>
        ) : null}
        {paused && !stopped ? <Badge tone="warn">Copying paused</Badge> : null}

        <div className="ml-auto flex items-center gap-3">
          <ConnectionDot
            status={connected ? "ONLINE" : "OFFLINE"}
            label={connected ? "Live" : "Reconnecting"}
          />

          {can("ADMIN") ? (
            <div className="flex items-center gap-1.5">
              {paused ? (
                <Button size="sm" variant="secondary" onClick={() => setConfirm("resume")}>
                  <PlayCircle className="h-3.5 w-3.5" />
                  Resume
                </Button>
              ) : (
                <Button size="sm" variant="secondary" onClick={() => setConfirm("pause")}>
                  <PauseCircle className="h-3.5 w-3.5" />
                  Pause copying
                </Button>
              )}
              <Button
                size="sm"
                variant="danger"
                disabled={stopped}
                onClick={() => setConfirm("stop")}
              >
                <OctagonX className="h-3.5 w-3.5" />
                Emergency stop
              </Button>
            </div>
          ) : null}

          <div className="flex items-center gap-2 border-l border-line pl-3">
            <div className="text-right leading-tight">
              <p className="text-xs font-medium">{user?.email}</p>
              <p className="text-2xs text-fg-subtle">{user?.role.replace("_", " ")}</p>
            </div>
            <Button size="icon" variant="ghost" onClick={logout} aria-label="Sign out">
              <LogOut className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </header>

      <ConfirmPhraseDialog
        open={confirm === "stop"}
        onClose={() => setConfirm(null)}
        onConfirm={() => mutate.mutate("stop")}
        title="Emergency stop"
        description={
          "Halts all copy activity immediately. Member EAs refuse instructions on their " +
          "next poll. Open positions are NOT closed — mass-closing on a panic click is " +
          "more dangerous than stopping."
        }
        phrase="EMERGENCY STOP"
        confirmLabel="Stop everything"
        pending={mutate.isPending}
      />

      <ConfirmPhraseDialog
        open={confirm === "pause"}
        onClose={() => setConfirm(null)}
        onConfirm={() => mutate.mutate("pause")}
        title="Pause copying"
        description="New copy orders will be recorded and cancelled. Existing positions are left alone, and the Telegram channel keeps publishing."
        phrase="PAUSE"
        confirmLabel="Pause"
        tone="warn"
        pending={mutate.isPending}
      />

      <ConfirmPhraseDialog
        open={confirm === "resume"}
        onClose={() => setConfirm(null)}
        onConfirm={() => mutate.mutate("resume")}
        title="Resume copying"
        description="New master trades will start being copied to enabled members again."
        phrase="RESUME"
        confirmLabel="Resume"
        tone="primary"
        pending={mutate.isPending}
      />
    </>
  );
}
