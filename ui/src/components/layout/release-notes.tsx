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
} from "@/components/ui/dialog";
import { SidebarMenuItem } from "@/components/ui/sidebar";
import {
  formatReleaseDate,
  hasViewedRelease,
  markReleaseAsViewed,
  releaseNotes,
  type ReleaseNoteSection,
} from "@/lib/release-notes";

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

interface ReleaseNotesSectionProps {
  section: ReleaseNoteSection;
  featured?: boolean;
  className?: string;
}

function ReleaseNotesSection({ section, featured = false, className = "" }: ReleaseNotesSectionProps) {
  const headingId = `release-notes-${section.id}`;

  return (
    <article
      aria-labelledby={headingId}
      className={`rounded-xl p-5 sm:p-6 ${
        featured ? "border border-primary/15 bg-primary/[0.04]" : "border border-border/60 bg-muted/35"
      } ${className}`}
    >
      {featured && <p className="text-xs font-semibold uppercase tracking-[0.14em] text-primary">Featured</p>}
      <h4 id={headingId} className={`${featured ? "mt-2 text-lg" : "text-sm"} font-semibold text-foreground`}>
        {section.title}
      </h4>
      <ul className={`${featured ? "mt-4 grid gap-3 sm:grid-cols-2" : "mt-3 space-y-3"} text-sm leading-6 text-muted-foreground`}>
        {section.items.map((entry) => (
          <li key={entry} className="flex gap-2.5">
            <span
              aria-hidden="true"
              className={`mt-2 size-1.5 shrink-0 rounded-full ${featured ? "bg-primary" : "bg-muted-foreground/55"}`}
            />
            <span>{entry}</span>
          </li>
        ))}
      </ul>
    </article>
  );
}

export function ReleaseNotesDialog({ open, onOpenChange }: ReleaseNotesDialogProps) {
  const highlights = releaseNotes.sections.find((section) => section.id === "highlights");
  const supportingSections = releaseNotes.sections.filter((section) => section.id !== "highlights");

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
          <div className="space-y-6">
            <section aria-labelledby="release-notes-whats-new">
              <h3 id="release-notes-whats-new" className="text-base font-semibold text-foreground">
                What's New
              </h3>
              <div className="mt-4 space-y-4">
                {highlights && <ReleaseNotesSection section={highlights} featured />}
                <div className="grid gap-4 sm:grid-cols-2">
                  {supportingSections.map((section) => (
                    <ReleaseNotesSection
                      key={section.id}
                      section={section}
                      className={section.id === "openai-api" ? "sm:col-span-2" : ""}
                    />
                  ))}
                </div>
              </div>
            </section>

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
