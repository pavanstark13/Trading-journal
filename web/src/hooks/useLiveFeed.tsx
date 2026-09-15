"use client";

import { useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { API_BASE, getAccessToken } from "@/lib/api";
import type { WsMessage } from "@/lib/types";

const MAX_EVENTS = 60;

interface LiveValue {
  connected: boolean;
  events: WsMessage[];
  health: Record<string, string>;
}

const LiveContext = React.createContext<LiveValue>({
  connected: false,
  events: [],
  health: {},
});

/**
 * One WebSocket for the whole console. Messages both feed the activity strip and
 * invalidate the relevant React Query caches, so tables refresh themselves without
 * anyone polling.
 */
export function LiveFeedProvider({ children }: { children: React.ReactNode }) {
  const [connected, setConnected] = React.useState(false);
  const [events, setEvents] = React.useState<WsMessage[]>([]);
  const [health, setHealth] = React.useState<Record<string, string>>({});
  const queryClient = useQueryClient();

  React.useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let closed = false;

    const connect = () => {
      const token = getAccessToken();
      if (!token) {
        reconnectTimer = setTimeout(connect, 1000);
        return;
      }
      const url = `${API_BASE.replace(/^http/, "ws")}/api/v1/ws?token=${encodeURIComponent(token)}`;
      socket = new WebSocket(url);

      socket.onopen = () => {
        attempt = 0;
        setConnected(true);
        socket?.send(JSON.stringify({ type: "subscribe", topics: ["trades", "copy", "health"] }));
      };

      socket.onmessage = (raw) => {
        let message: WsMessage;
        try {
          message = JSON.parse(raw.data);
        } catch {
          return;
        }
        if (message.type === "pong") return;

        if (message.type === "health") {
          setHealth(message.data as Record<string, string>);
          return;
        }

        setEvents((previous) => [message, ...previous].slice(0, MAX_EVENTS));

        // Tell React Query what changed rather than refetching everything.
        if (message.type === "trade.event") {
          queryClient.invalidateQueries({ queryKey: ["trades"] });
          queryClient.invalidateQueries({ queryKey: ["dashboard"] });
        } else if (message.type === "copy.update") {
          queryClient.invalidateQueries({ queryKey: ["copy-orders"] });
          queryClient.invalidateQueries({ queryKey: ["dashboard"] });
        } else if (message.type === "ea.status") {
          queryClient.invalidateQueries({ queryKey: ["ea-installations"] });
          queryClient.invalidateQueries({ queryKey: ["members"] });
        }
      };

      socket.onclose = () => {
        setConnected(false);
        if (closed) return;
        // Exponential backoff, capped: a reconnect storm during an outage helps nobody.
        const delay = Math.min(1000 * 2 ** attempt++, 30000);
        reconnectTimer = setTimeout(connect, delay);
      };

      socket.onerror = () => socket?.close();
    };

    connect();
    const ping = setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "ping" }));
      }
    }, 25000);

    return () => {
      closed = true;
      clearInterval(ping);
      if (reconnectTimer) clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [queryClient]);

  const value = React.useMemo(
    () => ({ connected, events, health }),
    [connected, events, health],
  );
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

export function useLiveFeed() {
  return React.useContext(LiveContext);
}
