"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight } from "lucide-react";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import { CalendarHeatmap } from "@/components/charts/calendar-heatmap";
import {
  Button, Card, CardBody, CardHeader, ErrorNote, Field, Skeleton,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { cn, num, signed } from "@/lib/utils";

interface Curves {
  daily: { date: string; trades: number; net_profit: string; wins: number; losses: number }[];
}

export default function CalendarPage() {
  const [month, setMonth] = React.useState(() => {
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth(), 1);
  });
  const [selected, setSelected] = React.useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["curves"],
    queryFn: () => api.get<Curves>("/stats/curves"),
  });

  const monthKey = `${month.getFullYear()}-${String(month.getMonth() + 1).padStart(2, "0")}`;
  const monthDays = (data?.daily ?? []).filter((d) => d.date.startsWith(monthKey));
  const monthTotal = monthDays.reduce((sum, d) => sum + Number.parseFloat(d.net_profit), 0);
  const tradingDays = monthDays.length;
  const greenDays = monthDays.filter((d) => Number.parseFloat(d.net_profit) > 0).length;

  const note = useDailyNote(selected);

  return (
    <>
      <PageHeader
        title="Calendar"
        description="Every trading day at a glance. Click a day to write your review."
      />

      <div className="grid gap-4 lg:grid-cols-[1fr_20rem]">
        <Card>
          <CardHeader
            title={month.toLocaleDateString(undefined, { month: "long", year: "numeric" })}
            action={
              <div className="flex gap-1">
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label="Previous month"
                  onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}
                >
                  <ChevronLeft className="h-4 w-4" />
                </Button>
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label="Next month"
                  onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}
                >
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </div>
            }
          />
          <CardBody>
            {error ? (
              <ErrorNote error={error} />
            ) : isLoading ? (
              <Skeleton className="h-80" />
            ) : (
              <CalendarHeatmap days={monthDays} month={month} onSelectDay={setSelected} />
            )}
          </CardBody>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHeader title="This month" />
            <CardBody className="space-y-2 text-sm">
              <div className="flex items-center justify-between">
                <span className="text-fg-muted">Result</span>
                <span
                  className={cn(
                    "tabular text-lg font-semibold",
                    monthTotal > 0 && "text-long",
                    monthTotal < 0 && "text-short",
                  )}
                >
                  {signed(monthTotal)}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-fg-muted">Trading days</span>
                <span className="tabular">{tradingDays}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-fg-muted">Green days</span>
                <span className="tabular">
                  {greenDays}
                  {tradingDays ? ` of ${tradingDays}` : ""}
                </span>
              </div>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title={selected ? `Review · ${selected}` : "Daily review"} />
            <CardBody>
              {selected ? (
                <div className="space-y-3">
                  <Field label="Plan before the session">
                    <textarea
                      rows={3}
                      value={note.preMarket}
                      onChange={(event) => note.setPreMarket(event.target.value)}
                      placeholder="What you were watching, and what you would and would not take…"
                      className="w-full rounded-md border border-line bg-bg-sunken px-3 py-2 text-sm placeholder:text-fg-subtle focus:border-accent focus:outline-none"
                    />
                  </Field>
                  <Field label="How it actually went">
                    <textarea
                      rows={3}
                      value={note.postMarket}
                      onChange={(event) => note.setPostMarket(event.target.value)}
                      placeholder="Honest review of the day…"
                      className="w-full rounded-md border border-line bg-bg-sunken px-3 py-2 text-sm placeholder:text-fg-subtle focus:border-accent focus:outline-none"
                    />
                  </Field>
                  <Button
                    variant="primary"
                    disabled={!note.dirty || note.saving}
                    onClick={note.save}
                  >
                    {note.saving ? "Saving…" : "Save review"}
                  </Button>
                </div>
              ) : (
                <p className="text-sm text-fg-muted">
                  Pick a day on the calendar to write your review of it.
                </p>
              )}
            </CardBody>
          </Card>
        </div>
      </div>
    </>
  );
}

function useDailyNote(date: string | null) {
  const [preMarket, setPreMarket] = React.useState("");
  const [postMarket, setPostMarket] = React.useState("");
  const [dirty, setDirty] = React.useState(false);
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => {
    if (!date) return;
    let cancelled = false;
    setDirty(false);
    api
      .get<{ pre_market: string | null; post_market: string | null }>(`/daily-notes/${date}`)
      .then((data) => {
        if (cancelled) return;
        setPreMarket(data.pre_market ?? "");
        setPostMarket(data.post_market ?? "");
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [date]);

  return {
    preMarket,
    postMarket,
    dirty,
    saving,
    setPreMarket: (value: string) => {
      setPreMarket(value);
      setDirty(true);
    },
    setPostMarket: (value: string) => {
      setPostMarket(value);
      setDirty(true);
    },
    save: async () => {
      if (!date) return;
      setSaving(true);
      try {
        await api.put(`/daily-notes/${date}`, {
          pre_market: preMarket || null,
          post_market: postMarket || null,
        });
        setDirty(false);
      } finally {
        setSaving(false);
      }
    },
  };
}
