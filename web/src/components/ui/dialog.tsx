"use client";

import * as React from "react";

import { Button } from "./primitives";
import { cn } from "@/lib/utils";

export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children?: React.ReactNode;
  footer?: React.ReactNode;
}) {
  React.useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="w-full max-w-md rounded-lg border border-line bg-bg-raised shadow-xl"
      >
        <div className="border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold">{title}</h2>
          {description ? (
            <p className="mt-1 text-xs text-fg-muted">{description}</p>
          ) : null}
        </div>
        {children ? <div className="space-y-3 p-4">{children}</div> : null}
        {footer ? (
          <div className="flex justify-end gap-2 border-t border-line px-4 py-3">{footer}</div>
        ) : null}
      </div>
    </div>
  );
}

/**
 * Destructive actions require the operator to type an exact phrase.
 * A button you can hit by accident is not a safeguard, and these actions stop
 * or start real order flow.
 */
export function ConfirmPhraseDialog({
  open,
  onClose,
  onConfirm,
  title,
  description,
  phrase,
  confirmLabel = "Confirm",
  tone = "danger",
  pending,
}: {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  description: string;
  phrase: string;
  confirmLabel?: string;
  tone?: "danger" | "warn" | "primary";
  pending?: boolean;
}) {
  const [typed, setTyped] = React.useState("");
  React.useEffect(() => {
    if (open) setTyped("");
  }, [open]);

  const matches = typed === phrase;

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      description={description}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant={tone}
            disabled={!matches || pending}
            onClick={onConfirm}
          >
            {pending ? "Working…" : confirmLabel}
          </Button>
        </>
      }
    >
      <p className="text-xs text-fg-muted">
        Type <code className="rounded bg-bg-sunken px-1 font-mono text-fg">{phrase}</code>{" "}
        to continue.
      </p>
      <input
        autoFocus
        value={typed}
        onChange={(event) => setTyped(event.target.value)}
        className={cn(
          "h-9 w-full rounded-md border bg-bg-sunken px-3 font-mono text-sm outline-none",
          matches ? "border-long" : "border-line",
        )}
        placeholder={phrase}
      />
    </Dialog>
  );
}
