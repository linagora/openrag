import { useState } from "react";
import { Copy, Check } from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";

interface UuidCellProps {
  value: string | null | undefined;
  /** When true, render only the leading segment plus an ellipsis (default true). */
  truncate?: boolean;
  className?: string;
}

/**
 * Compact UUID display with click-to-copy. Shows the first 8 chars + ellipsis
 * when truncated; full value goes to the clipboard. Hover the cell to reveal
 * the icon; click anywhere on the cell to copy.
 */
export function UuidCell({ value, truncate = true, className }: UuidCellProps) {
  const [copied, setCopied] = useState(false);

  if (!value) return <span className="text-muted-foreground">—</span>;

  const display = truncate ? `${value.slice(0, 8)}…` : value;

  const onCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      toast.success("UUID copied");
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("Could not copy");
    }
  };

  return (
    <button
      type="button"
      onClick={onCopy}
      title={value}
      className={cn(
        "group inline-flex items-center gap-1.5 font-mono text-xs text-muted-foreground hover:text-foreground transition-colors",
        className,
      )}
    >
      <span>{display}</span>
      {copied ? (
        <Check className="h-3 w-3 text-green-600" />
      ) : (
        <Copy className="h-3 w-3 opacity-0 group-hover:opacity-100 transition-opacity" />
      )}
    </button>
  );
}
