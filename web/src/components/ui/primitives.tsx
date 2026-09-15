"use client";

import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/lib/utils";

/* ── Card ──────────────────────────────────────────────────────────────────── */

export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "rounded-lg border border-line bg-bg-raised shadow-sm shadow-black/20",
        className,
      )}
      {...props}
    />
  );
}

export function CardHeader({
  title,
  action,
  className,
}: {
  title: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex items-center justify-between gap-3 border-b border-line px-4 py-3",
        className,
      )}
    >
      <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
      {action}
    </div>
  );
}

export function CardBody({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-4", className)} {...props} />;
}

/* ── Button ────────────────────────────────────────────────────────────────── */

const buttonStyles = cva(
  "inline-flex items-center justify-center gap-2 rounded-md text-sm font-medium " +
    "transition-colors focus-visible:outline-none focus-visible:ring-2 " +
    "focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg " +
    "disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        primary: "bg-accent text-accent-fg hover:brightness-110",
        secondary: "border border-line bg-bg-raised hover:bg-bg-sunken",
        ghost: "hover:bg-bg-raised",
        danger: "bg-danger text-white hover:brightness-110",
        warn: "bg-warn text-black hover:brightness-110",
      },
      size: {
        sm: "h-7 px-2.5 text-xs",
        md: "h-9 px-3.5",
        lg: "h-10 px-5",
        icon: "h-8 w-8",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonStyles> {}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button ref={ref} className={cn(buttonStyles({ variant, size }), className)} {...props} />
  ),
);
Button.displayName = "Button";

/* ── Badge ─────────────────────────────────────────────────────────────────── */

const badgeStyles = cva(
  "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-2xs font-semibold uppercase tracking-wide",
  {
    variants: {
      tone: {
        neutral: "bg-bg-sunken text-fg-muted ring-1 ring-inset ring-line",
        good: "bg-long/15 text-long ring-1 ring-inset ring-long/30",
        warn: "bg-warn/15 text-warn ring-1 ring-inset ring-warn/30",
        bad: "bg-danger/15 text-danger ring-1 ring-inset ring-danger/30",
        info: "bg-accent/15 text-accent ring-1 ring-inset ring-accent/30",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export function Badge({
  className,
  tone,
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeStyles>) {
  return <span className={cn(badgeStyles({ tone }), className)} {...props} />;
}

/** One place decides what each status colour means, so the whole console agrees. */
export function StatusBadge({ status }: { status: string }) {
  const tone = (
    {
      EXECUTED: "good", ONLINE: "good", SENT: "info", ACTIVE: "good",
      PENDING: "warn", WARNING: "warn", RETRY: "warn", PAPER: "info",
      FAILED: "bad", REJECTED: "bad", TIMED_OUT: "bad", OFFLINE: "bad",
      DEAD: "bad", FAIL: "bad", SUSPENDED: "bad", REVOKED: "bad",
      CANCELLED: "neutral", IGNORED: "neutral", UNKNOWN: "neutral",
      OK: "good", LIVE: "good", PROCESSED: "good", EDITED: "good",
    } as const
  )[status] ?? "neutral";
  return <Badge tone={tone}>{status.replace(/_/g, " ")}</Badge>;
}

export function SideBadge({ side }: { side: string | null }) {
  if (!side) return <span className="text-fg-subtle">—</span>;
  return (
    <span
      className={cn(
        "font-semibold",
        side === "BUY" ? "text-long" : "text-short",
      )}
    >
      {side}
    </span>
  );
}

/* ── Connection dot ────────────────────────────────────────────────────────── */

export function ConnectionDot({
  status,
  label,
}: {
  status: string;
  label?: string;
}) {
  const colour =
    status === "ONLINE"
      ? "bg-long"
      : status === "WARNING"
        ? "bg-warn"
        : status === "OFFLINE"
          ? "bg-danger"
          : "bg-fg-subtle";
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={cn(
          "h-2 w-2 rounded-full",
          colour,
          status === "ONLINE" && "animate-pulse-dot",
        )}
      />
      {label ? <span className="text-xs text-fg-muted">{label}</span> : null}
    </span>
  );
}

/* ── Inputs ────────────────────────────────────────────────────────────────── */

export const Input = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(({ className, ...props }, ref) => (
  <input
    ref={ref}
    className={cn(
      "h-9 w-full rounded-md border border-line bg-bg-sunken px-3 text-sm",
      "placeholder:text-fg-subtle focus:border-accent focus:outline-none",
      "disabled:opacity-50",
      className,
    )}
    {...props}
  />
));
Input.displayName = "Input";

export const Select = React.forwardRef<
  HTMLSelectElement,
  React.SelectHTMLAttributes<HTMLSelectElement>
>(({ className, ...props }, ref) => (
  <select
    ref={ref}
    className={cn(
      "h-9 w-full rounded-md border border-line bg-bg-sunken px-2 text-sm",
      "focus:border-accent focus:outline-none",
      className,
    )}
    {...props}
  />
));
Select.displayName = "Select";

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block space-y-1.5">
      <span className="text-xs font-medium text-fg-muted">{label}</span>
      {children}
      {hint ? <span className="block text-2xs text-fg-subtle">{hint}</span> : null}
    </label>
  );
}

export function Toggle({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative h-5 w-9 shrink-0 rounded-full transition-colors disabled:opacity-50",
        checked ? "bg-accent" : "bg-bg-sunken ring-1 ring-inset ring-line",
      )}
    >
      <span
        className={cn(
          "absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform",
          checked ? "translate-x-4" : "translate-x-0.5",
        )}
      />
    </button>
  );
}

/* ── Table ─────────────────────────────────────────────────────────────────── */

export function Table({ className, ...props }: React.TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="overflow-x-auto">
      <table className={cn("w-full text-sm", className)} {...props} />
    </div>
  );
}

export function Th({ className, ...props }: React.ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      className={cn(
        "border-b border-line px-3 py-2 text-left text-2xs font-semibold uppercase",
        "tracking-wider text-fg-subtle",
        className,
      )}
      {...props}
    />
  );
}

export function Td({ className, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td className={cn("border-b border-line/60 px-3 py-2 align-middle", className)} {...props} />
  );
}

export function EmptyState({
  title,
  hint,
}: {
  title: string;
  hint?: string;
}) {
  return (
    <div className="px-4 py-12 text-center">
      <p className="text-sm text-fg-muted">{title}</p>
      {hint ? <p className="mt-1 text-xs text-fg-subtle">{hint}</p> : null}
    </div>
  );
}

/* ── Feedback ──────────────────────────────────────────────────────────────── */

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded bg-bg-sunken", className)} />;
}

export function ErrorNote({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : "Something went wrong";
  return (
    <div className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
      {message}
    </div>
  );
}

export function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tone?: "good" | "bad" | "warn";
}) {
  return (
    <Card className="p-4">
      <p className="text-2xs font-semibold uppercase tracking-wider text-fg-subtle">
        {label}
      </p>
      <p
        className={cn(
          "tabular mt-1.5 text-2xl font-semibold leading-none",
          tone === "good" && "text-long",
          tone === "bad" && "text-danger",
          tone === "warn" && "text-warn",
        )}
      >
        {value}
      </p>
      {sub ? <p className="mt-1.5 text-xs text-fg-muted">{sub}</p> : null}
    </Card>
  );
}
