import { useMemo, useRef, useState, type KeyboardEvent } from "react";
import { CheckIcon, ChevronDownIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { PartitionResponse } from "@/lib/api/partitions";
import { cn } from "@/lib/utils";

// Popover height around the rows: search input, group padding, border and offset.
const POPOVER_CHROME_HEIGHT = 50;
const ROW_HEIGHT = 32;
const LIST_MAX_HEIGHT = 300;
// Below this there's too little room under the trigger for a usable list.
const MIN_POPOVER_HEIGHT = 180;

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
  const [side, setSide] = useState<"top" | "bottom">("bottom");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const sorted = useMemo(() => [...partitions].sort((a, b) => a.name.localeCompare(b.name)), [partitions]);
  const selected = partitions.find((p) => p.partition === value);

  const handleOpenChange = (next: boolean) => {
    const trigger = triggerRef.current;
    if (next && trigger) {
      const roomBelow = () => window.innerHeight - trigger.getBoundingClientRect().bottom;
      const popoverHeight = POPOVER_CHROME_HEIGHT + Math.min(LIST_MAX_HEIGHT, partitions.length * ROW_HEIGHT);
      if (roomBelow() < popoverHeight) trigger.scrollIntoView({ block: "center" });
      // Scrolling can't help at the end of a page, so only then open above.
      setSide(roomBelow() < Math.min(popoverHeight, MIN_POPOVER_HEIGHT) ? "top" : "bottom");
    }
    setOpen(next);
  };

  // A Select opens on the arrow keys; PopoverTrigger only handles Enter and Space.
  const handleTriggerKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      handleOpenChange(true);
    }
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
          onKeyDown={handleTriggerKeyDown}
          className={cn("justify-between font-normal", className)}
        >
          <span className={cn("truncate", !selected && "text-muted-foreground")}>{selected?.name ?? "Select partition"}</span>
          <ChevronDownIcon className="size-4 opacity-50" />
        </Button>
      </PopoverTrigger>
      {/* Side is chosen on open: automatic flipping would put long lists over the page header.
          Disabling collisions also disables shifting, so size to the space that's left. */}
      <PopoverContent
        side={side}
        align="start"
        avoidCollisions={false}
        className="w-(--radix-popover-trigger-width) min-w-[min(18rem,var(--radix-popover-content-available-width))] p-0"
      >
        <Command defaultValue={value}>
          <CommandInput placeholder="Search partitions..." />
          <CommandList className="max-h-[min(300px,calc(var(--radix-popover-content-available-height)-38px))]">
            <CommandEmpty>No partition found.</CommandEmpty>
            <CommandGroup>
              {sorted.map((p) => (
                <CommandItem
                  key={p.partition}
                  value={p.partition}
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
