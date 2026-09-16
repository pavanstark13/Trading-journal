"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { PageHeader } from "@/components/app/page-header";
import { BarBreakdown } from "@/components/charts/bar-breakdown";
import { Dialog } from "@/components/ui/dialog";
import {
  Badge, Button, Card, CardBody, CardHeader, EmptyState, ErrorNote,
  Field, Input, Skeleton,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { Bucket, Setup, TagRow } from "@/lib/types";

/**
 * The playbook gives the analytics something to group by. Without named setups,
 * "which of my strategies actually works" is unanswerable.
 */
export default function PlaybookPage() {
  const queryClient = useQueryClient();
  const [adding, setAdding] = React.useState(false);

  const setups = useQuery({
    queryKey: ["setups"],
    queryFn: () => api.get<Setup[]>("/setups"),
  });
  const tags = useQuery({
    queryKey: ["tags"],
    queryFn: () => api.get<TagRow[]>("/tags"),
  });
  const bySetup = useQuery({
    queryKey: ["breakdown", "setup"],
    queryFn: () => api.get<{ buckets: Bucket[] }>("/stats/breakdown/setup"),
  });

  return (
    <>
      <PageHeader
        title="Playbook"
        description="Name your setups, then find out which ones actually make money."
        action={<Button variant="primary" onClick={() => setAdding(true)}>Add a setup</Button>}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Your setups" />
          {setups.isLoading ? (
            <CardBody><Skeleton className="h-32" /></CardBody>
          ) : (setups.data ?? []).length === 0 ? (
            <EmptyState
              title="No setups yet"
              hint="Add the patterns you actually trade. Then tag each trade with one, and the numbers will tell you which to keep."
            />
          ) : (
            <CardBody className="space-y-3">
              {(setups.data ?? []).map((setup) => (
                <div key={setup.id} className="rounded-md border border-line px-3 py-2.5">
                  <p className="text-sm font-medium">{setup.name}</p>
                  {setup.description ? (
                    <p className="mt-0.5 text-xs text-fg-muted">{setup.description}</p>
                  ) : null}
                  {setup.checklist.length > 0 ? (
                    <ul className="mt-2 space-y-0.5">
                      {setup.checklist.map((item) => (
                        <li key={item} className="text-xs text-fg-muted">— {item}</li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              ))}
            </CardBody>
          )}
        </Card>

        <Card>
          <CardHeader title="Which setups make money" />
          <CardBody>
            {bySetup.isLoading ? (
              <Skeleton className="h-32" />
            ) : (
              <BarBreakdown
                buckets={bySetup.data?.buckets ?? []}
                label="Tag trades with a setup to fill this in"
                emptyMessage="No trades tagged with a setup yet"
              />
            )}
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardHeader title="Tags" />
        <CardBody>
          <TagManager tags={tags.data ?? []} />
        </CardBody>
      </Card>

      <AddSetupDialog
        open={adding}
        onClose={() => setAdding(false)}
        onSaved={() => {
          setAdding(false);
          queryClient.invalidateQueries({ queryKey: ["setups"] });
        }}
      />
    </>
  );
}

function TagManager({ tags }: { tags: TagRow[] }) {
  const queryClient = useQueryClient();
  const [name, setName] = React.useState("");

  const create = useMutation({
    mutationFn: () => api.post("/tags", { name }),
    onSuccess: () => {
      setName("");
      queryClient.invalidateQueries({ queryKey: ["tags"] });
    },
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-1.5">
        {tags.length === 0 ? (
          <p className="text-sm text-fg-muted">
            No tags yet. Tags are looser than setups — use them for anything you want to
            slice by later, like &quot;news day&quot; or &quot;A+ entry&quot;.
          </p>
        ) : (
          tags.map((tag) => <Badge key={tag.id} tone="info">{tag.name}</Badge>)
        )}
      </div>
      <div className="flex gap-2">
        <Input
          className="max-w-xs"
          value={name}
          placeholder="New tag"
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && name.trim()) create.mutate();
          }}
        />
        <Button
          variant="secondary"
          disabled={!name.trim() || create.isPending}
          onClick={() => create.mutate()}
        >
          Add
        </Button>
      </div>
      {create.error ? <ErrorNote error={create.error} /> : null}
    </div>
  );
}

function AddSetupDialog({
  open, onClose, onSaved,
}: {
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [checklist, setChecklist] = React.useState("");

  React.useEffect(() => {
    if (open) {
      setName("");
      setDescription("");
      setChecklist("");
    }
  }, [open]);

  const save = useMutation({
    mutationFn: () =>
      api.post("/setups", {
        name,
        description: description || null,
        checklist: checklist
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean),
      }),
    onSuccess: onSaved,
  });

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Add a setup"
      description="A setup is a pattern you trade on purpose, with rules you can check."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!name.trim() || save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <Field label="Name">
        <Input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="London breakout"
        />
      </Field>
      <Field label="What is it?">
        <textarea
          rows={2}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="Break of the Asia range in the first hour of London, with a retest."
          className="w-full rounded-md border border-line bg-bg-sunken px-3 py-2 text-sm placeholder:text-fg-subtle focus:border-accent focus:outline-none"
        />
      </Field>
      <Field label="Your rules" hint="One per line. These are what you grade yourself against.">
        <textarea
          rows={4}
          value={checklist}
          onChange={(event) => setChecklist(event.target.value)}
          placeholder={"Asia range is clean\nBreak holds on the retest\nStop below the range\nAt least 2R to target"}
          className="w-full rounded-md border border-line bg-bg-sunken px-3 py-2 text-sm placeholder:text-fg-subtle focus:border-accent focus:outline-none"
        />
      </Field>
      {save.error ? <ErrorNote error={save.error} /> : null}
    </Dialog>
  );
}
