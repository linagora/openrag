import { useState } from "react";
import type { LucideIcon } from "lucide-react";
import { AlertTriangle, Braces, Bug, CalendarDays, FileSearch, Gauge, Sparkles } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { SidebarMenuItem } from "@/components/ui/sidebar";
import {
  formatReleaseDate,
  hasViewedRelease,
  markReleaseAsViewed,
  releaseNotes,
  type ReleaseNoteSection,
  type ReleaseNoteSectionId,
} from "@/lib/release-notes";

const releaseNoteSectionIcons: Record<ReleaseNoteSectionId, LucideIcon> = {
  highlights: Sparkles,
  "openai-api": Braces,
  indexing: FileSearch,
  improvements: Gauge,
  fixes: Bug,
};

interface ReleaseNotesButtonProps {
  hasNew: boolean;
  isOpen: boolean;
  onOpen: () => void;
  className: string;
}

export function ReleaseNotesButton({ hasNew, isOpen, onOpen, className }: ReleaseNotesButtonProps) {
  const label = `Release Notes · v${releaseNotes.version}`;

  return (
    <SidebarMenuItem className="group-data-[collapsible=icon]:hidden">
      <button
        type="button"
        title={hasNew ? `${label} — New` : label}
        aria-label={hasNew ? `Open ${label}, new` : `Open ${label}`}
        aria-haspopup="dialog"
        aria-expanded={isOpen}
        onClick={onOpen}
        className={`${className} ${
          isOpen
            ? "bg-sidebar-accent text-sidebar-accent-foreground shadow-sm"
            : "text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground"
        }`}
      >
        <span className="min-w-0 flex-1 truncate">{label}</span>
        {hasNew && (
          <span className="ml-auto shrink-0 rounded-full bg-sidebar-primary/15 px-1.5 py-0.5 text-[0.6rem] font-semibold leading-none text-sidebar-primary">
            New
          </span>
        )}
      </button>
    </SidebarMenuItem>
  );
}

interface ReleaseNotesDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function ReleaseNotesSection({ section }: { section: ReleaseNoteSection }) {
  const Icon = releaseNoteSectionIcons[section.id];
  const headingId = `release-notes-${section.id}`;

  return (
    <section aria-labelledby={headingId} className="rounded-xl border bg-card p-4 shadow-sm">
      <div className="flex items-start gap-3">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <Icon aria-hidden="true" className="size-4" />
        </span>
        <div className="min-w-0">
          <h3 id={headingId} className="text-sm font-semibold text-foreground">
            {section.title}
          </h3>
          <ul className="mt-2 space-y-2 text-sm leading-6 text-muted-foreground">
            {section.items.map((entry) => (
              <li key={entry} className="flex gap-2">
                <span aria-hidden="true" className="mt-2 size-1.5 shrink-0 rounded-full bg-primary/70" />
                <span>{entry}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}

export function ReleaseNotesDialog({ open, onOpenChange }: ReleaseNotesDialogProps) {
  const highlights = releaseNotes.sections.find((section) => section.id === "highlights");
  const remainingSections = releaseNotes.sections.filter((section) => section.id !== "highlights");

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
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

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-6 py-6 sm:px-8" data-testid="release-notes-content">
          <div className="space-y-4">
            {highlights && <ReleaseNotesSection section={highlights} />}

            <section aria-labelledby="release-notes-breaking-changes">
              <Alert className="border-amber-200 bg-amber-50 text-amber-950 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100">
                <AlertTriangle aria-hidden="true" className="text-amber-700 dark:text-amber-300" />
                <AlertTitle id="release-notes-breaking-changes" className="text-amber-950 dark:text-amber-100">
                  {releaseNotes.breakingChanges.title}
                </AlertTitle>
                <AlertDescription className="text-amber-900/90 dark:text-amber-100/90">
                  <p>
                    <span className="font-semibold">{releaseNotes.breakingChanges.calloutTitle}.</span>{" "}
                    {releaseNotes.breakingChanges.description}
                  </p>
                  <p className="font-medium">{releaseNotes.breakingChanges.action}</p>
                </AlertDescription>
              </Alert>
            </section>

            <div className="grid gap-4 sm:grid-cols-2">
              {remainingSections.map((section) => (
                <ReleaseNotesSection key={section.id} section={section} />
              ))}
            </div>
          </div>
        </div>

        <DialogFooter className="shrink-0 border-t bg-background px-6 py-4 sm:px-8" showCloseButton />
      </DialogContent>
    </Dialog>
  );
}

interface ReleaseNotesProps {
  className: string;
}

export function ReleaseNotes({ className }: ReleaseNotesProps) {
  const [open, setOpen] = useState(false);
  const [hasNew, setHasNew] = useState(() => !hasViewedRelease(releaseNotes.version));

  const handleOpenChange = (nextOpen: boolean) => {
    setOpen(nextOpen);
    if (nextOpen) {
      markReleaseAsViewed(releaseNotes.version);
      setHasNew(false);
    }
  };

  return (
    <>
      <ReleaseNotesButton hasNew={hasNew} isOpen={open} onOpen={() => handleOpenChange(true)} className={className} />
      <ReleaseNotesDialog open={open} onOpenChange={handleOpenChange} />
    </>
  );
}
