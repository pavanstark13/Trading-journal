"use client";

import {
  BarChart3, BookOpen, CalendarDays, LayoutDashboard,
  Plug, Settings, Target,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

const NAV = [
  { href: "/dashboard", label: "Overview", icon: LayoutDashboard },
  { href: "/trades", label: "Trades", icon: BookOpen },
  { href: "/calendar", label: "Calendar", icon: CalendarDays },
  { href: "/analytics", label: "Analytics", icon: BarChart3 },
  { href: "/playbook", label: "Playbook", icon: Target },
  { href: "/accounts", label: "Accounts", icon: Plug },
  { href: "/settings", label: "Settings", icon: Settings },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="hidden w-52 shrink-0 flex-col border-r border-line bg-bg-sunken md:flex">
      <div className="flex h-14 items-center gap-2 border-b border-line px-4">
        <span className="h-2.5 w-2.5 rounded-full bg-accent" />
        <span className="text-sm font-semibold tracking-tight">Trading Journal</span>
      </div>

      <nav className="flex-1 space-y-0.5 px-2 py-4">
        {NAV.map((item) => {
          const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors",
                active
                  ? "bg-accent/15 font-medium text-accent"
                  : "text-fg-muted hover:bg-bg-raised hover:text-fg",
              )}
            >
              <Icon className="h-4 w-4 shrink-0" />
              {item.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
