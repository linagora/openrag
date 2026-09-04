import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { getVersion } from "@/lib/api/system";
import { hasNewIn, isNew } from "@/lib/whats-new";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * Attaching a "NEW" marker. When one is shown is `lib/whats-new.ts`; the worked
 * guide, with examples for each kind of surface, is FEATURE_TAG.md at the repo
 * root.
 *
 * Nothing here is tied to a page or a control:
 *
 *   - `<NewBadge feature="models.reranker" />` renders the marker, or nothing,
 *     wherever an element can go — a nav item, a tab, a card title, a field
 *     label, a button.
 *   - `useNewOptions(group, values)` covers a list of options, where the marker
 *     has to appear twice: on each new option, and on the collapsed control,
 *     since a marker that only exists inside an opened dropdown aids no
 *     discovery. The group is named once and each option's key is derived from
 *     it, so registering an option in `whats-new.ts` is the whole change. Its
 *     `dot` is the collapsed-control marker for a dense form label, `badge` the
 *     same thing spelled out where there is room.
 *   - `useIsNew(feature)` is the same decision as a plain boolean, for a surface
 *     that marks itself some other way.
 */

/**
 * The running app version, shared across every badge on the page.
 *
 * `staleTime: Infinity` because the version cannot change without a reload, and
 * the query key matches the one already used by the overview page so the two
 * share a single request.
 */
function useAppVersion(): string | undefined {
  const { data } = useQuery({
    queryKey: ["version"],
    queryFn: getVersion,
    staleTime: Infinity,
    retry: false,
  });
  return data?.version;
}

/**
 * Shared sizing for the marker. `default` is the brand accent (`--primary`,
 * `#c71f45`) — the same red as the primary action button, so the badge reads as
 * "ours" rather than as a warning. Note `--primary` is only red in the light
 * theme; `.dark` redefines it to a blue-violet, so the badge follows the accent
 * rather than staying literally red across both.
 *
 * The last rule is what lets the marker be a plain child anywhere, selects
 * included. Radix drives a closed select trigger from the selected option's
 * own children, so a marker on an option is mirrored into the trigger as well
 * — a second NEW beside the one on the control's label. Rather than give every
 * such control a slot to thread the marker past, the marker hides the mirrored
 * copy itself: `[data-slot=select-value] .…:hidden`. Pair it with `textValue`
 * on the item, which keeps the marker out of Radix's typeahead text.
 */
const BADGE_CLASS =
  "px-1.5 py-0 text-[10px] font-semibold tracking-wide [[data-slot=select-value]_&]:hidden";

/**
 * The marker itself, shared by every entry point above so that "NEW" is spelled,
 * sized and coloured in exactly one place.
 */
function NewMarker({ className }: { className?: string }) {
  return (
    <Badge variant="default" className={cn(BADGE_CLASS, className)}>
      NEW
    </Badge>
  );
}

/**
 * The quiet variant: a dot rather than a word, for a *label* whose control is
 * collapsed. It says "there is something in here" without widening the label or
 * competing with the field beside it, and the reader gets the word itself — the
 * NEW on the option — the moment they open the list.
 *
 * Amber rather than the brand accent: a crimson dot beside a form label reads as
 * "required" or "invalid" (the red-asterisk convention), which is the one thing
 * it must not mean. It carries a label of its own, because a bare dot tells a
 * screen reader nothing and a sighted reader not much more.
 */
export function NewDot({
  className,
  label = "New options available",
}: {
  className?: string;
  label?: string;
}) {
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      // `inline-block` is load-bearing: width and height do not apply to a
      // non-replaced inline element, so a bare span would render 0x0 anywhere
      // its parent is not a flex or grid container. It costs nothing where the
      // parent *is* one — a flex item is blockified regardless.
      //
      // Then `self-start` positions it in a flex label and `align-super` in an
      // inline one; each is ignored in the other context, so it sits high
      // either way without the caller arranging anything.
      className={cn(
        "inline-block size-1.5 shrink-0 self-start rounded-full bg-amber-400 align-super dark:bg-amber-300",
        className
      )}
    />
  );
}

/** Whether `feature` is still new on this deployment. */
export function useIsNew(feature: string): boolean {
  return isNew(feature, useAppVersion());
}

/**
 * A "NEW" marker that expires on its own — see `lib/whats-new.ts`.
 *
 * Renders nothing when the feature is no longer new or when the registry has no
 * entry for it. An unreadable app version silences a *pinned* entry — there is
 * nothing to compare it against — but not an `UNRELEASED` one, which has no
 * version to compare in the first place.
 */
export function NewBadge({ feature, className }: { feature: string; className?: string }) {
  return useIsNew(feature) ? <NewMarker className={className} /> : null;
}

/**
 * Markers for a list of options belonging to `group` — the keys looked up are
 * `"<group>.<value>"`.
 *
 * `badge` is for the collapsed control (the label of a select, the summary of a
 * disclosure): present when *any* option is new, so the reader learns there is
 * something to find without opening it. `badgeFor(value)` is for the option
 * itself. Both are `null` when there is nothing new, so a caller can drop them
 * in unconditionally.
 *
 * `values` comes from whatever the surface actually renders — usually the API's
 * option list — so an option this backend doesn't offer is never badged, even
 * while the registry carries a key for it.
 */
export function useNewOptions(
  group: string,
  values: string[]
): {
  anyNew: boolean;
  dot: ReactNode;
  badge: ReactNode;
  badgeFor: (value: string) => ReactNode;
} {
  const version = useAppVersion();
  const anyNew = hasNewIn(group, values, version);
  return {
    anyNew,
    dot: anyNew ? <NewDot /> : null,
    badge: anyNew ? <NewMarker /> : null,
    badgeFor: (value: string) => (isNew(`${group}.${value}`, version) ? <NewMarker /> : null),
  };
}
