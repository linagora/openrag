import { useState } from "react";
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

export function ReleaseNotesDialog({ open, onOpenChange }: ReleaseNotesDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[calc(100vh-2rem)] flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl">
        <DialogHeader className="shrink-0 border-b px-6 py-5 pr-14">
          <DialogTitle>OpenRAG v{releaseNotes.version}</DialogTitle>
          <DialogDescription>{releaseNotes.summary}</DialogDescription>
          <p className="text-xs font-medium text-muted-foreground">Released: {formatReleaseDate(releaseNotes.date)}</p>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5" data-testid="release-notes-content">
          <section aria-labelledby="release-notes-whats-new">
            <h3 id="release-notes-whats-new" className="text-sm font-semibold text-foreground">
              What's New
            </h3>
            <ul className="mt-2 space-y-2 text-sm leading-6 text-muted-foreground">
              {releaseNotes.whatsNew.map((entry) => (
                <li key={entry} className="relative pl-4 before:absolute before:left-0 before:text-sidebar-primary before:content-['•']">
                  {entry}
                </li>
              ))}
            </ul>
          </section>
        </div>

        <DialogFooter className="shrink-0 border-t px-6 py-4" showCloseButton />
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
