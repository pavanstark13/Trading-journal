"use client";

import { useRouter } from "next/navigation";
import * as React from "react";

import { Sidebar } from "@/components/app/sidebar";
import { TopBar } from "@/components/app/top-bar";
import { Skeleton } from "@/components/ui/primitives";
import { useAuth } from "@/hooks/useAuth";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const router = useRouter();

  React.useEffect(() => {
    if (!loading && !user) router.replace("/login");
  }, [loading, user, router]);

  if (loading) {
    return (
      <div className="flex min-h-screen">
        <Skeleton className="h-screen w-52" />
        <div className="flex-1 space-y-4 p-6">
          <Skeleton className="h-9 w-64" />
          <Skeleton className="h-64 w-full" />
        </div>
      </div>
    );
  }
  if (!user) return null;

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <main className="flex-1 space-y-5 p-5">{children}</main>
      </div>
    </div>
  );
}
