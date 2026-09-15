"use client";

import {
  Activity, AlertTriangle, BarChart3, Cpu, FileClock, Gauge,
  Radio, Send, Settings, ShieldAlert, Users,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useAuth } from "@/hooks/useAuth";
import type { Role } from "@/lib/types";
import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  minRole: Role;
}

const NAV: { section: string; items: NavItem[] }[] = [
  {
    section: "Operations",
    items: [
      { href: "/dashboard", label: "Overview", icon: Gauge, minRole: "MEMBER" },
      { href: "/trades", label: "Trades", icon: BarChart3, minRole: "ADMIN" },
      { href: "/copy-orders", label: "Copy Orders", icon: Activity, minRole: "MEMBER" },
    ],
  },
  {
    section: "Accounts",
    items: [
      { href: "/master-account", label: "Master Account", icon: Radio, minRole: "ADMIN" },
      { href: "/members", label: "Members", icon: Users, minRole: "MEMBER" },
      { href: "/ea-installations", label: "EA Installations", icon: Cpu, minRole: "ADMIN" },
    ],
  },
  {
    section: "Control",
    items: [
      { href: "/risk", label: "Risk", icon: ShieldAlert, minRole: "ADMIN" },
      { href: "/telegram", label: "Telegram", icon: Send, minRole: "ADMIN" },
      { href: "/system-health", label: "System Health", icon: AlertTriangle, minRole: "ADMIN" },
      { href: "/audit-logs", label: "Audit Log", icon: FileClock, minRole: "ADMIN" },
      { href: "/settings", label: "Settings", icon: Settings, minRole: "ADMIN" },
    ],
  },
];

export function Sidebar() {
  const pathname = usePathname();
  const { can } = useAuth();

  return (
    <aside className="hidden w-56 shrink-0 flex-col border-r border-line bg-bg-sunken md:flex">
      <div className="flex h-14 items-center gap-2 border-b border-line px-4">
        <span className="h-2.5 w-2.5 rounded-full bg-accent" />
        <span className="text-sm font-semibold tracking-tight">TradeBridge</span>
      </div>

      <nav className="flex-1 space-y-5 overflow-y-auto px-2 py-4">
        {NAV.map((group) => {
          const visible = group.items.filter((item) => can(item.minRole));
          if (visible.length === 0) return null;
          return (
            <div key={group.section}>
              <p className="px-2 pb-1.5 text-2xs font-semibold uppercase tracking-wider text-fg-subtle">
                {group.section}
              </p>
              <ul className="space-y-0.5">
                {visible.map((item) => {
                  const active = pathname === item.href;
                  const Icon = item.icon;
                  return (
                    <li key={item.href}>
                      <Link
                        href={item.href}
                        className={cn(
                          "flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm transition-colors",
                          active
                            ? "bg-accent/15 font-medium text-accent"
                            : "text-fg-muted hover:bg-bg-raised hover:text-fg",
                        )}
                      >
                        <Icon className="h-4 w-4 shrink-0" />
                        {item.label}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </nav>
    </aside>
  );
}
