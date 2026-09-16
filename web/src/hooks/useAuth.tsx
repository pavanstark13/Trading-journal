"use client";

import { useRouter } from "next/navigation";
import * as React from "react";

import { api, setAccessToken } from "@/lib/api";
import type { LoginResponse, Role, User } from "@/lib/types";

interface AuthValue {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string, totp?: string) => Promise<void>;
  logout: () => Promise<void>;
  can: (minimum: Role) => boolean;
}

const RANK: Record<Role, number> = { MEMBER: 0, ADMIN: 1 };

const AuthContext = React.createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = React.useState<User | null>(null);
  const [loading, setLoading] = React.useState(true);
  const router = useRouter();

  // On mount the access token is gone (it lives in memory only), but the refresh
  // cookie may still be valid -- so try once before deciding the user is logged out.
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      if (await api.refresh()) {
        try {
          const me = await api.get<User>("/auth/me");
          if (!cancelled) setUser(me);
        } catch {
          /* fall through to logged out */
        }
      }
      if (!cancelled) setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = React.useCallback(
    async (email: string, password: string, totp?: string) => {
      const data = await api.post<LoginResponse>("/auth/login", {
        email,
        password,
        totp_code: totp || null,
      });
      setAccessToken(data.access_token);
      setUser(data.user);
    },
    [],
  );

  const logout = React.useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } finally {
      setAccessToken(null);
      setUser(null);
      router.push("/login");
    }
  }, [router]);

  const can = React.useCallback(
    (minimum: Role) => (user ? RANK[user.role] >= RANK[minimum] : false),
    [user],
  );

  const value = React.useMemo(
    () => ({ user, loading, login, logout, can }),
    [user, loading, login, logout, can],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = React.useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
