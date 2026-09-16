"use client";

import { useRouter } from "next/navigation";
import * as React from "react";

import { Button, Card, ErrorNote, Field, Input } from "@/components/ui/primitives";
import { useAuth } from "@/hooks/useAuth";
import { ApiError } from "@/lib/api";

export default function LoginPage() {
  const { login, user, loading } = useAuth();
  const router = useRouter();
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [totp, setTotp] = React.useState("");
  const [needsTotp, setNeedsTotp] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const [pending, setPending] = React.useState(false);

  React.useEffect(() => {
    if (!loading && user) router.replace("/dashboard");
  }, [loading, user, router]);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setPending(true);
    try {
      await login(email, password, totp || undefined);
      router.replace("/dashboard");
    } catch (caught) {
      if (caught instanceof ApiError && /totp/i.test(caught.message)) {
        setNeedsTotp(true);
      }
      setError(caught);
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm p-6">
        <div className="mb-6">
          <div className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-full bg-accent" />
            <h1 className="text-lg font-semibold tracking-tight">Trading Journal</h1>
          </div>
          <p className="mt-1 text-xs text-fg-muted">
            Your MetaTrader trades, written up and measured
          </p>
        </div>

        <form onSubmit={onSubmit} className="space-y-4">
          <Field label="Email">
            <Input
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </Field>
          <Field label="Password">
            <Input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </Field>
          {needsTotp ? (
            <Field label="Authenticator code" hint="6 digits from your authenticator app">
              <Input
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={8}
                value={totp}
                onChange={(event) => setTotp(event.target.value)}
              />
            </Field>
          ) : null}

          {error ? <ErrorNote error={error} /> : null}

          <Button type="submit" variant="primary" className="w-full" disabled={pending}>
            {pending ? "Signing in…" : "Sign in"}
          </Button>
        </form>
      </Card>
    </main>
  );
}
