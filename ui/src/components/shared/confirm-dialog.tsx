import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import type { ReactNode } from "react";

// Need to add alert-dialog component
export { AlertDialog, AlertDialogTrigger };

interface ConfirmDialogProps {
  title: string;
  description: string;
  onConfirm: () => void;
  children: ReactNode;
  destructive?: boolean;
  onOpenChange?: (open: boolean) => void;
}

export function ConfirmDialog({
  title,
  description,
  onConfirm,
  children,
  destructive = true,
  onOpenChange,
}: ConfirmDialogProps) {
  return (
    <AlertDialog onOpenChange={onOpenChange}>
      <AlertDialogTrigger asChild>{children}</AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader className="min-w-0">
          <AlertDialogTitle className="min-w-0 [overflow-wrap:anywhere]">{title}</AlertDialogTitle>
          <AlertDialogDescription
            // Focusable so a long description can be scrolled from the keyboard
            // (Safari does not focus scroll containers on Tab).
            tabIndex={0}
            className="min-w-0 max-h-[50vh] overflow-y-auto rounded-sm [overflow-wrap:anywhere] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {description}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={onConfirm}
            className={destructive ? "bg-destructive text-white hover:bg-destructive/90" : ""}
          >
            Confirm
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
