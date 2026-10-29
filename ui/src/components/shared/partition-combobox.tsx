import { useMemo, useRef, useState } from "react";
import { CheckIcon, ChevronDownIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { PartitionResponse } from "@/lib/api/partitions";
import { cn } from "@/lib/utils";

// Popover height: search input plus the list's 300px cap.
const POPOVER_HEIGHT = 350;

interface PartitionComboboxProps {
  partitions: readonly Pick<PartitionResponse, "partition" | "name">[];
  value: string;
  onValueChange: (partition: string) => void;
  className?: string;
  "aria-label"?: string;
}

/** Partition picker with type-to-filter, for deployments with many partitions. */
export function PartitionCombobox({
  partitions,
  value,
  onValueChange,
  className,
  "aria-label": ariaLabel,
}: PartitionComboboxProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const sorted = useMemo(() => [...partitions].sort((a, b) => a.name.localeCompare(b.name)), [partitions]);
  const selected = partitions.find((p) => p.partition === value);

  const handleOpenChange = (next: boolean) => {
    // The list never flips above, so make room under the trigger before it opens.
    const trigger = triggerRef.current;
    if (next && trigger && window.innerHeight - trigger.getBoundingClientRect().bottom < POPOVER_HEIGHT) {
      trigger.scrollIntoView({ block: "center" });
    }
    setOpen(next);
  };

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <Button
          ref={triggerRef}
          variant="outline"
          role="combobox"
          aria-expanded={open}
          aria-label={ariaLabel}
          className={cn("justify-between font-normal", className)}
        >
          <span className={cn("truncate", !selected && "text-muted-foreground")}>{selected?.name ?? "Select partition"}</span>
          <ChevronDownIcon className="size-4 opacity-50" />
        </Button>
      </PopoverTrigger>
      {/* Always below the trigger: a long list flipped above it covers the page header. */}
      <PopoverContent
        side="bottom"
        align="start"
        avoidCollisions={false}
        className="w-(--radix-popover-trigger-width) min-w-72 p-0"
      >
        <Command defaultValue={value}>
          <CommandInput placeholder="Search partitions..." />
          <CommandList className="max-h-[min(300px,max(8rem,calc(var(--radix-popover-content-available-height)-3rem)))]">
            <CommandEmpty>No partition found.</CommandEmpty>
            <CommandGroup>
              {sorted.map((p) => (
                <CommandItem
                  key={p.partition}
                  value={p.partition}
                  keywords={[p.name]}
                  onSelect={() => {
                    // Like a Select: picking the current partition is not a change.
                    if (p.partition !== value) onValueChange(p.partition);
                    setOpen(false);
                  }}
                >
                  <span className="truncate">{p.name}</span>
                  <CheckIcon className={cn("ml-auto", p.partition === value ? "opacity-100" : "opacity-0")} />
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
