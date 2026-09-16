"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check } from "lucide-react";
import * as React from "react";

import {
  Badge, Button, ErrorNote, Field, Select, Toggle,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { JournalData, Setup } from "@/lib/types";
import { cn } from "@/lib/utils";

const GRADES = ["A", "B", "C", "D", "F"];

/**
 * The only part of a trade a human types.
 *
 * Everything factual is already filled in from the broker, so this asks for what
 * only the trader knows: why they took it, and whether they followed their own plan.
 *
 * The grade is deliberately labelled as grading the *process*. A losing trade taken
 * correctly is an A; a winner taken on a whim is not.
 */
export function JournalEditor({
  tradeId,
  journal,
  onSaved,
}: {
  tradeId: string;
  journal: JournalData | null;
  onSaved?: () => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = React.useState({
    thesis: journal?.thesis ?? "",
    execution_notes: journal?.execution_notes ?? "",
    lesson: journal?.lesson ?? "",
    emotion: journal?.emotion ?? "",
    confidence: journal?.confidence ?? null as number | null,
    followed_plan: journal?.followed_plan ?? null as boolean | null,
    mistakes: journal?.mistakes ?? ([] as string[]),
    grade: journal?.grade ?? "",
    setup_id: journal?.setup_id ?? "",
  });
  const [dirty, setDirty] = React.useState(false);

  const vocabulary = useQuery({
    queryKey: ["journal-vocabulary"],
    queryFn: () => api.get<{ emotions: string[]; mistakes: string[] }>("/journal/vocabulary"),
    staleTime: Infinity,
  });
  const setups = useQuery({
    queryKey: ["setups"],
    queryFn: () => api.get<Setup[]>("/setups"),
  });

  const save = useMutation({
    mutationFn: () =>
      api.put(`/trades/${tradeId}/journal`, {
        thesis: form.thesis || null,
        execution_notes: form.execution_notes || null,
        lesson: form.lesson || null,
        emotion: form.emotion || null,
        confidence: form.confidence,
        followed_plan: form.followed_plan,
        mistakes: form.mistakes,
        grade: form.grade || null,
        setup_id: form.setup_id || null,
      }),
    onSuccess: () => {
      setDirty(false);
      queryClient.invalidateQueries({ queryKey: ["trade", tradeId] });
      queryClient.invalidateQueries({ queryKey: ["trades"] });
      queryClient.invalidateQueries({ queryKey: ["overview"] });
      onSaved?.();
    },
  });

  function update<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((previous) => ({ ...previous, [key]: value }));
    setDirty(true);
  }

  function toggleMistake(mistake: string) {
    update(
      "mistakes",
      form.mistakes.includes(mistake)
        ? form.mistakes.filter((m) => m !== mistake)
        : [...form.mistakes, mistake],
    );
  }

  return (
    <div className="space-y-4">
      <Field label="Why did you take it?">
        <textarea
          rows={3}
          value={form.thesis}
          onChange={(event) => update("thesis", event.target.value)}
          placeholder="The setup, the level, what made this one worth taking…"
          className="w-full rounded-md border border-line bg-bg-sunken px-3 py-2 text-sm placeholder:text-fg-subtle focus:border-accent focus:outline-none"
        />
      </Field>

      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Setup">
          <Select
            value={form.setup_id}
            onChange={(event) => update("setup_id", event.target.value)}
          >
            <option value="">No setup</option>
            {(setups.data ?? []).map((setup) => (
              <option key={setup.id} value={setup.id}>{setup.name}</option>
            ))}
          </Select>
        </Field>
        <Field label="How did you feel?">
          <Select
            value={form.emotion}
            onChange={(event) => update("emotion", event.target.value)}
          >
            <option value="">Not recorded</option>
            {(vocabulary.data?.emotions ?? []).map((emotion) => (
              <option key={emotion} value={emotion}>{emotion}</option>
            ))}
          </Select>
        </Field>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <p className="mb-1.5 text-xs font-medium text-fg-muted">Confidence at entry</p>
          <div className="flex gap-1">
            {[1, 2, 3, 4, 5].map((level) => (
              <button
                key={level}
                type="button"
                onClick={() => update("confidence", form.confidence === level ? null : level)}
                className={cn(
                  "h-8 flex-1 rounded-md border text-sm transition-colors",
                  form.confidence === level
                    ? "border-accent bg-accent/15 font-semibold text-accent"
                    : "border-line hover:bg-bg-sunken",
                )}
              >
                {level}
              </button>
            ))}
          </div>
        </div>

        <div>
          <p className="mb-1.5 text-xs font-medium text-fg-muted">
            Process grade
            <span className="ml-1 font-normal text-fg-subtle">— not the result</span>
          </p>
          <div className="flex gap-1">
            {GRADES.map((grade) => (
              <button
                key={grade}
                type="button"
                onClick={() => update("grade", form.grade === grade ? "" : grade)}
                className={cn(
                  "h-8 flex-1 rounded-md border text-sm font-semibold transition-colors",
                  form.grade === grade
                    ? "border-accent bg-accent/15 text-accent"
                    : "border-line hover:bg-bg-sunken",
                )}
              >
                {grade}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="flex items-center justify-between rounded-md border border-line px-3 py-2">
        <div>
          <p className="text-sm">Followed my plan</p>
          <p className="text-2xs text-fg-subtle">
            This is the statistic that turns discipline into a money figure.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {form.followed_plan === null ? (
            <span className="text-2xs text-fg-subtle">not set</span>
          ) : null}
          <Toggle
            checked={form.followed_plan === true}
            onChange={(next) => update("followed_plan", next)}
          />
        </div>
      </div>

      <div>
        <p className="mb-1.5 text-xs font-medium text-fg-muted">What went wrong?</p>
        <div className="flex flex-wrap gap-1.5">
          {(vocabulary.data?.mistakes ?? []).map((mistake) => {
            const active = form.mistakes.includes(mistake);
            return (
              <button key={mistake} type="button" onClick={() => toggleMistake(mistake)}>
                <Badge tone={active ? "bad" : "neutral"}>
                  {active ? <Check className="h-3 w-3" /> : null}
                  {mistake}
                </Badge>
              </button>
            );
          })}
        </div>
      </div>

      <Field label="What actually happened">
        <textarea
          rows={2}
          value={form.execution_notes}
          onChange={(event) => update("execution_notes", event.target.value)}
          placeholder="How the trade played out, and how you managed it…"
          className="w-full rounded-md border border-line bg-bg-sunken px-3 py-2 text-sm placeholder:text-fg-subtle focus:border-accent focus:outline-none"
        />
      </Field>

      <Field label="Lesson">
        <textarea
          rows={2}
          value={form.lesson}
          onChange={(event) => update("lesson", event.target.value)}
          placeholder="What would you do differently?"
          className="w-full rounded-md border border-line bg-bg-sunken px-3 py-2 text-sm placeholder:text-fg-subtle focus:border-accent focus:outline-none"
        />
      </Field>

      {save.error ? <ErrorNote error={save.error} /> : null}

      <div className="flex items-center gap-3">
        <Button variant="primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save note"}
        </Button>
        {!dirty && journal ? (
          <span className="flex items-center gap-1 text-xs text-long">
            <Check className="h-3.5 w-3.5" /> Saved
          </span>
        ) : null}
      </div>
    </div>
  );
}
