# Feature tagging — the **NEW** badge

A small marker that tells a reader of the admin UI "this wasn't here last time you looked", and
that **removes itself**.

## The idea

A NEW marker is only useful if someone takes it down. Nobody does. Written as markup, it becomes
permanent decoration: it tells the reader nothing, and the cleanup lands in a release that has no
other reason to touch the file.

So the marker is *data*. One registry — [`ui/src/lib/whats-new.ts`](ui/src/lib/whats-new.ts) — maps
a **feature key** to the version that feature shipped in, and the badge stops rendering once the
running app is far enough past that version. Forgetting to delete an entry costs nothing: it expires
on its own, and the dead line can be swept up whenever anyone happens to notice.

Two consequences worth knowing up front:

- **Nothing decides "which feature is newest."** Each entry carries its own anchor and answers the
  same independent question: *is the running version within the window of the version I shipped in?*
  Entries never interact, so adding one is never a re-ordering exercise.
- **Marking a feature is a one-line data change.** The JSX that renders a badge asks about a key; it
  does not know which keys are live. Adding an entry lights it up, and removing one puts it out.

## When a badge shows

The window is `NEW_FOR_MINORS` (currently **2**) minor releases wide, counted from the version in the
registry. For an entry pinned to `2.2.0`:

| app reports | badge | why |
|---|---|---|
| `2.1.1` | ✅ | older than the feature — it isn't there at all, so nothing renders anyway |
| `2.2.0` → `2.3.0` | ✅ | inside the window |
| `2.4.0` | ❌ | window passed |
| `3.0.0` | ❌ | a major bump always expires it |
| `UNRELEASED` entry | ✅ | merged but untagged — new to everyone running a build that has it |
| `unknown` / unreadable | ❌ | fails closed |

Failing closed is deliberate: one unbadged new feature beats *every* feature badged forever on a
deployment whose `/version` cannot be read. "Unreadable" means the whole string has to be a version,
not merely start like one — `2.2rubbish` is rejected rather than read as `2.2`. What counts is wider
than SemVer, because `/version` serves `importlib.metadata` and therefore PEP 440: `2.2.0rc1`,
`2.1.1.post1` and `2.2.0.dev3` all compare normally, so badges do not blank out on an RC deployment.

## Two markers: the dot and the word

The same registry entry can surface in two shapes, and which one you reach for is a question about
*where* the marker sits, not about the feature.

| | shape | says | use it on |
|---|---|---|---|
| **word** | `NEW` pill in the brand accent | *this, here, is the new thing* | the thing itself: an option in an open list, a tab, a nav item, a card header |
| **dot** | 6px amber circle, sitting high | *there is something in here* | a **collapsed** control's label, where the new thing is out of sight until the reader opens it |

A collapsed control is the case that needs both:

```text
Strategy •                     ← dot on the label: something inside is new
┌──────────────────────┐
│ recursive_splitter   │       ← closed trigger stays clean
└──────────────────────┘
   ▼ opened
   ┌────────────────────────────────┐
   │ recursive_splitter             │
   │ structured_section  [NEW]      │  ← the word, on the thing itself
   └────────────────────────────────┘
```

The dot gets the reader to open the list; the word tells them what they came for. A dot alone would
never say *what* is new, and the word alone would be invisible until they had already found it —
which is the discovery problem the marker exists to solve.

Three properties of the dot worth knowing:

- **It has a name.** It renders as `role="img"` with an accessible name (`"New options available"` by
  default, overridable with `label`), because a bare dot tells a screen reader nothing and a sighted
  reader not much more. It is also its own `title`, so hovering explains it.
- **It is amber, not the brand accent.** A crimson dot beside a form label reads as *required* or
  *invalid* — the red-asterisk convention — which is the one thing it must not mean. Amber is not
  free of associations either (this UI uses yellow for `QUEUED`/`PENDING` and amber for the
  super-admin banner), so if that collides in a new context, the colour is one class in `NewDot`.
- **It positions itself.** `inline-block` so that its 6px survives outside a flex row (width and
  height do not apply to an inline element); then `self-start` in a flex label and `align-super` in
  an inline one, each ignored in the other context, so it sits high either way without the caller
  arranging anything.

Both come from the same entry and expire on the same day. Nothing here animates: a 6px dot that
pulses in a settings form reads as an alert, not a hint.

## Feature keys

A key is any stable dotted string. Two conventions:

- **An option inside a control** → `"<group>.<value>"`, e.g. `"chunking.structured_section"`. The
  group scoping matters: two dropdowns can legitimately offer the same option string, and a bare key
  would badge both the day that happens.
- **Anything else** (a page, a tab, a section, an action) → whatever names it stably, e.g.
  `"models.reranker"`, `"nav.workspaces"`, `"presets.tab.retrieval"`.

## Tagging something

### 1. Register the key

```ts
// ui/src/lib/whats-new.ts
export const NEW_SINCE: Readonly<Record<string, string>> = {
  "chunking.structured_section": "UNRELEASED",
};
```

Always `UNRELEASED`, never a guessed version — see [Release time](#release-time) below.

### 2. Attach it

Everything below comes from [`ui/src/components/shared/new-badge.tsx`](ui/src/components/shared/new-badge.tsx).

#### A new option in a dropdown

The awkward case, and the only one with a rule of its own: a badge that lives *inside* an unopened
dropdown aids no discovery, because the reader has to be looking at it already. So the marker goes on
the collapsed control **as well as** the option. `useNewOptions` gives you both, and derives each
option's key from the group named once:

```tsx
const newChunking = useNewOptions("chunking", chunkingStrategies);

<Label className="flex items-center gap-1.5 text-xs">
  Strategy
  {newChunking.dot}             {/* set when *any* option below is new */}
</Label>
<Select value={…} onValueChange={…}>
  <SelectTrigger size="sm"><SelectValue /></SelectTrigger>
  <SelectContent>
    {chunkingStrategies.map((s) => (
      <SelectItem key={s} value={s} textValue={s}>
        <span className="flex items-center gap-1.5">
          {s}
          {newChunking.badgeFor(s)}
        </span>
      </SelectItem>
    ))}
  </SelectContent>
</Select>
```

`dot` keeps a dense form row narrow; swap in `badge` where there is room for the word on the
collapsed control — the two are interchangeable and expire together (see
[Two markers](#two-markers-the-dot-and-the-word)).

Two details that are easy to get wrong:

- **`textValue={s}`.** Radix derives an item's typeahead text from its children, so without it the
  badge joins the search string and the option answers to `"structured_sectionNEW"`.
- **`values` comes from what the surface actually renders** — here the API's option list — so an
  option this backend doesn't offer is never badged, even while the registry carries its key.

You do *not* need to keep the badge out of the closed trigger by hand. Radix mirrors the selected
option's children into the trigger; the marker hides that copy itself, which is what lets it be a
plain child here instead of being threaded through a slot on the select.

#### A new tab, section, page or action

Nothing special: render the marker where it belongs. It returns `null` when the feature isn't new, so
it can be dropped in unconditionally. (An unreadable version silences a *pinned* entry, since there is
nothing to compare it against; an `UNRELEASED` one still shows, having no version to compare in the
first place.)

A new model type in the endpoints page:

```tsx
// ui/src/pages/admin/models.tsx — MODEL_TYPES gained "reranker"
<TabsTrigger key={t} value={t} className="capitalize">
  {t}
  <NewBadge feature={`models.${t}`} />
</TabsTrigger>
```

…and the same component on a card header, a page header, or a sidebar entry:

```tsx
<CardTitle className="text-base">Endpoints <NewBadge feature="models.endpoints" /></CardTitle>
```

```tsx
<span className="truncate">{item.title}</span>
<NewBadge feature={`nav.${item.href}`} />
```

A dot outside a dropdown — a nav entry whose new page is one click away, say — is the same component
gated by the key yourself:

```tsx
{useIsNew("nav.workspaces") && <NewDot label="New page" />}
```

#### Checking your work

An `UNRELEASED` entry badges regardless of what `/version` reports, so `npm run dev` in `ui/` shows
the marker on its own — no backend needed. A *pinned* entry needs a readable version to compare
against, and renders nothing without one.

#### Just the decision

For a surface that wants to mark itself some other way — a dot, an ordering nudge, an expanded-by-
default section:

```tsx
const isNew = useIsNew("models.reranker");
```

## Release time

The version a feature ships in **is not knowable when its PR is written**: a `release/X.Y.Z` branch is
cut once the features for that version are complete, so the number is chosen afterwards, sometimes
weeks later. Any pin written during development is a prediction, and wrong the moment the release plan
moves.

So entries are authored as `UNRELEASED` and resolved by the release branch, in the same commit that
bumps `pyproject.toml` — reading the version back from that file rather than retyping it, so the two
cannot diverge. The command lives in **CONTRIBUTING.md → Releases → Bumping the version**, and only
there, so the two cannot drift.

An unresolved entry badges every user forever, and silently — nobody inspects a dropdown expecting an
option *not* to say NEW. So `verify-tag` in [`.github/workflows/build.yml`](.github/workflows/build.yml)
refuses to publish GA images while any entry still reads `UNRELEASED`. Forgetting the step fails the
release instead of shipping a permanent badge.

### What the gate does not cover

`verify-tag` runs on GA tags only (`refs/tags/v*`, RC tags excluded — `build_rc.yml` owns those). So an
`UNRELEASED` entry badges freely on `develop`, on RC images and on anything self-built, and nothing
stops it there.

That is the design rather than a hole: `UNRELEASED` means *merged but not yet in a release*, and
everyone running such a build is in fact seeing the feature for the first time. What the gate
guarantees is narrower and is the part that matters — an unresolved entry cannot reach a GA release,
where it would badge forever. Between merge and release the marker is correct; the exposure is bounded
by the next GA tag, which cannot happen while any entry is still unresolved.

Both the release one-liner and the gate anchor on a whole registry line — indent, quoted key, colon,
and an optional trailing comma, since the last entry in the object may legally omit it. That is why
the commented examples in `whats-new.ts` are invisible to them, and why a looser pattern must not be
substituted: it would rewrite the `UNRELEASED` constant itself, turning the
sentinel into a version and breaking every future entry.

## Retiring an entry

Optional, and never urgent — an expired entry renders nothing. Delete the line whenever you notice it;
no test or workflow depends on any particular key being present.

## Where the pieces live

| file | role |
|---|---|
| `ui/src/lib/whats-new.ts` | the registry, the window rule, and nothing about the UI |
| `ui/src/components/shared/new-badge.tsx` | `NewBadge`, `NewDot`, `useNewOptions`, `useIsNew` — attaching a marker |
| `ui/src/lib/whats-new.test.ts` | the window rule and the registry lookup |
| `ui/src/components/shared/new-badge.test.tsx` | attachment, including the select-trigger case |
| `.github/workflows/build.yml` | the `UNRELEASED` gate on GA tags |
| `CONTRIBUTING.md` | the release-time command that resolves entries |
