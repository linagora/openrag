import { useState } from "react";
import { AlertTriangle, CalendarDays, Sparkles } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  formatReleaseDate,
  hasViewedRelease,
  markReleaseAsViewed,
  releaseNotes,
} from "@/lib/release-notes";
import { sidebarItemInactiveClassName } from "./sidebar-item";

const triggerClassName = `flex h-7 w-full items-center gap-1.5 rounded-md px-2 text-xs font-medium transition-colors ${sidebarItemInactiveClassName} data-[state=open]:bg-sidebar-accent data-[state=open]:text-sidebar-accent-foreground data-[state=open]:shadow-sm`;

export function ReleaseNotes() {
  const [hasNew, setHasNew] = useState(() => !hasViewedRelease(releaseNotes.version));
  const label = `Release Notes · v${releaseNotes.version}`;
  const { breakingChange } = releaseNotes;

  const handleOpenChange = (open: boolean) => {
    if (!open) return;
    markReleaseAsViewed(releaseNotes.version);
    setHasNew(false);
  };

  return (
    <Dialog onOpenChange={handleOpenChange}>
      <DialogTrigger
        className={triggerClassName}
        title={hasNew ? `${label} — New` : label}
        aria-label={hasNew ? `Open ${label}, new` : `Open ${label}`}
      >
        <span className="min-w-0 flex-1 truncate">{label}</span>
        {hasNew && (
          <span className="ml-auto shrink-0 rounded-full bg-sidebar-primary/15 px-1.5 py-0.5 text-[0.6rem] font-semibold leading-none text-sidebar-primary">
            New
          </span>
        )}
      </DialogTrigger>
      <DialogContent className="flex max-h-[calc(100vh-2rem)] flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl">
        <DialogHeader className="shrink-0 border-b bg-gradient-to-br from-primary/10 via-background to-background px-6 py-6 pr-14 sm:px-8">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <Badge className="bg-primary text-primary-foreground">
              <Sparkles aria-hidden="true" />
              Latest release
            </Badge>
            <p className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <CalendarDays aria-hidden="true" className="size-3.5" />
              Released {formatReleaseDate(releaseNotes.date)}
            </p>
          </div>
          <DialogTitle className="mt-3 text-2xl tracking-tight sm:text-3xl">
            OpenRAG <span className="text-primary">v{releaseNotes.version}</span>
          </DialogTitle>
          <DialogDescription className="max-w-2xl text-sm leading-6">{releaseNotes.summary}</DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 space-y-6 overflow-y-auto overscroll-contain px-6 py-6 sm:px-8">
          <section aria-labelledby="release-notes-whats-new">
            <h3 id="release-notes-whats-new" className="text-base font-semibold text-foreground">
              What's New
            </h3>
            <article aria-labelledby="release-notes-new-features" className="mt-4 rounded-xl border border-border/60 bg-muted/30 p-5 sm:p-6">
              <h4 id="release-notes-new-features" className="text-lg font-semibold text-foreground">
                New Features
              </h4>
              <ul className="mt-4 space-y-3 text-sm leading-6 text-muted-foreground">
                {releaseNotes.newFeatures.map((feature) => (
                  <li key={feature} className="flex gap-3">
                    <span aria-hidden="true" className="mt-2 size-1.5 shrink-0 rounded-full bg-primary" />
                    <span>{feature}</span>
                  </li>
                ))}
              </ul>
            </article>
          </section>

          {breakingChange && (
            <section aria-labelledby="release-notes-breaking-changes">
              <Alert className="border-amber-200 bg-amber-50 text-amber-950 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100">
                <AlertTriangle aria-hidden="true" className="text-amber-700 dark:text-amber-300" />
                <AlertTitle id="release-notes-breaking-changes" className="text-amber-950 dark:text-amber-100">
                  Breaking Changes
                </AlertTitle>
                <AlertDescription className="text-amber-900/90 dark:text-amber-100/90">
                  <p>
                    <span className="font-semibold">{breakingChange.title}.</span> {breakingChange.description}
                  </p>
                  <p className="font-medium">{breakingChange.action}</p>
                </AlertDescription>
              </Alert>
            </section>
          )}
        </div>

        <DialogFooter className="shrink-0 border-t bg-background px-6 py-4 sm:px-8" showCloseButton />
      </DialogContent>
    </Dialog>
  );
}
