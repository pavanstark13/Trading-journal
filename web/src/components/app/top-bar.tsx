"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, LogOut } from "lucide-react";
import Link from "next/link";

import { Badge, Button } from "@/components/ui/primitives";
import { useAuth } from "@/hooks/useAuth";
import { api } from "@/lib/api";
import type { AccountRow } from "@/lib/types";
import { relativeTime } from "@/lib/utils";

/**
 * The bar answers the one question a trader has on opening the page: is my
 * journal actually up to date? A silently stale journal is worse than an empty one.
 */
export function TopBar() {
  const { user, logout } = useAuth();

  const { data: accounts } = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api.get<AccountRow[]>("/accounts"),
    refetchInterval: 60_000,
  });

  const live = (accounts ?? []).filter((a) => !a.is_archived);
  const stale = live.filter((a) => !a.connected && a.ea && !a.ea.awaiting_setup);
  const pending = live.filter((a) => a.ea?.awaiting_setup);

  return (
    <>
      {stale.length > 0 ? (
        <Link
          href="/accounts"
          className="flex items-center justify-center gap-2 bg-warn/15 px-4 py-1.5 text-xs font-medium text-warn hover:bg-warn/20"
        >
          <AlertTriangle className="h-3.5 w-3.5" />
          {stale.length === 1
            ? `${stale[0].label} last synced ${relativeTime(stale[0].last_heartbeat_at)} — is MetaTrader running?`
            : `${stale.length} accounts have stopped syncing`}
        </Link>
      ) : null}

      <header className="flex h-14 shrink-0 items-center gap-3 border-b border-line bg-bg-raised px-4">
        {pending.length > 0 ? (
          <Link href="/accounts">
            <Badge tone="info">
              {pending.length} account{pending.length === 1 ? "" : "s"} waiting to connect
            </Badge>
          </Link>
        ) : live.length > 0 ? (
          <Badge tone="good">
            {live.filter((a) => a.connected).length}/{live.length} connected
          </Badge>
        ) : null}

        <div className="ml-auto flex items-center gap-2 border-l border-line pl-3">
          <div className="text-right leading-tight">
            <p className="text-xs font-medium">{user?.email}</p>
            <p className="text-2xs text-fg-subtle">{user?.timezone}</p>
          </div>
          <Button size="icon" variant="ghost" onClick={logout} aria-label="Sign out">
            <LogOut className="h-4 w-4" />
          </Button>
        </div>
      </header>
    </>
  );
}
