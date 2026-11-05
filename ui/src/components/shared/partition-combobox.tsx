import { useMemo, useState, type KeyboardEvent } from "react";
import { CheckIcon, ChevronDownIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { PartitionResponse } from "@/lib/api/partitions";
import { cn } from "@/lib/utils";

// Keep the list clear of the app header when it opens upward.
const HEADER_HEIGHT = 56;
const ROW_HEIGHT = 32;
const GROUP_PADDING = 8;
const SEARCH_INPUT_HEIGHT = 38;
// Room for the "No partition found." message.
const MIN_LIST_HEIGHT = 76;

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
  const sorted = useMemo(() => [...partitions].sort((a, b) => a.name.localeCompare(b.name)), [partitions]);
  const selected = partitions.find((p) => p.partition === value);

  // A Select opens on the arrow keys; PopoverTrigger only handles Enter and Space.
  const handleTriggerKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      setOpen(true);
    }
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
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
      <PopoverContent
        align="start"
        collisionPadding={{ top: HEADER_HEIGHT }}
        className="w-(--radix-popover-trigger-width) min-w-[min(18rem,var(--radix-popover-content-available-width))] p-0"
      >
        <Command defaultValue={value}>
          <CommandInput placeholder="Search partitions..." />
          {/* Fixed height, so filtering can't resize the popover and flip it to the other side. */}
          <CommandList
            style={{
              height: `min(${Math.max(sorted.length * ROW_HEIGHT + GROUP_PADDING, MIN_LIST_HEIGHT)}px, calc(var(--radix-popover-content-available-height) - ${SEARCH_INPUT_HEIGHT}px))`,
            }}
          >
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
