"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import {
  Button, Card, CardBody, CardHeader, ErrorNote, Field, Input, Select, Skeleton,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";

interface Profile {
  id: string;
  email: string;
  full_name: string | null;
  role: string;
  timezone: string;
  session_windows: Record<string, [string, string]>;
  totp_enabled: boolean;
}

// A short, practical list beats every IANA zone in a dropdown nobody can scroll.
const TIMEZONES = [
  "UTC", "Europe/London", "Europe/Berlin", "Europe/Moscow", "Asia/Dubai",
  "Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney",
  "America/New_York", "America/Chicago", "America/Los_Angeles",
];

export default function SettingsPage() {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["profile"],
    queryFn: () => api.get<Profile>("/settings/profile"),
  });

  const [timezone, setTimezone] = React.useState("");
  const [fullName, setFullName] = React.useState("");
  const [dirty, setDirty] = React.useState(false);

  React.useEffect(() => {
    if (!data) return;
    setTimezone(data.timezone);
    setFullName(data.full_name ?? "");
    setDirty(false);
  }, [data]);

  const save = useMutation({
    mutationFn: () =>
      api.put<{ accounts_rebuilt: number }>("/settings/profile", {
        timezone,
        full_name: fullName || null,
      }),
    onSuccess: () => {
      setDirty(false);
      queryClient.invalidateQueries({ queryKey: ["profile"] });
      // Time buckets are computed in the trader's timezone, so every statistic moves.
      queryClient.invalidateQueries({ queryKey: ["overview"] });
      queryClient.invalidateQueries({ queryKey: ["breakdown"] });
      queryClient.invalidateQueries({ queryKey: ["trades"] });
    },
  });

  if (error) return <ErrorNote error={error} />;
  if (isLoading || !data) return <Skeleton className="h-64" />;

  return (
    <>
      <PageHeader title="Settings" />

      <Card>
        <CardHeader title="You" />
        <CardBody className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Name">
              <Input
                value={fullName}
                onChange={(event) => {
                  setFullName(event.target.value);
                  setDirty(true);
                }}
              />
            </Field>
            <Field label="Email">
              <Input value={data.email} disabled />
            </Field>
          </div>

          <Field
            label="Your timezone"
            hint="Statistics like 'profit by hour' are worked out in this timezone, not the broker's. Changing it recalculates your existing trades."
          >
            <Select
              value={timezone}
              onChange={(event) => {
                setTimezone(event.target.value);
                setDirty(true);
              }}
            >
              {TIMEZONES.map((zone) => (
                <option key={zone} value={zone}>{zone}</option>
              ))}
            </Select>
          </Field>

          {save.data && save.data.accounts_rebuilt > 0 ? (
            <p className="text-xs text-long">
              Saved. Recalculated {save.data.accounts_rebuilt} account
              {save.data.accounts_rebuilt === 1 ? "" : "s"} against the new timezone.
            </p>
          ) : null}
          {save.error ? <ErrorNote error={save.error} /> : null}

          <Button variant="primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Trading sessions" />
        <CardBody>
          <p className="mb-3 text-sm text-fg-muted">
            Used for the session breakdown. Times are UTC, because session boundaries
            are conventionally quoted that way.
          </p>
          <dl className="grid gap-2 sm:grid-cols-2">
            {Object.entries(data.session_windows).map(([name, span]) => (
              <div
                key={name}
                className="flex items-center justify-between rounded-md border border-line px-3 py-2 text-sm"
              >
                <dt className="capitalize text-fg-muted">{name}</dt>
                <dd className="tabular">{span[0]} – {span[1]}</dd>
              </div>
            ))}
          </dl>
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Your data" />
        <CardBody className="space-y-2 text-sm text-fg-muted">
          <p>
            Every trade here is rebuilt from what your broker reported, which is stored
            untouched. If a calculation is ever wrong, it can be fixed and recalculated
            without you losing anything you wrote.
          </p>
          <p>
            Your notes are kept separately from the calculated trades, so recalculating
            never deletes them.
          </p>
        </CardBody>
      </Card>
    </>
  );
}
