# chat-openrag — v2 Preparation

**Date:** 2026-06-26
**Status:** Preparation notes (v1 shipped, v2 not started)
**Author:** Paul Tran-Van (consolidated with Claude during v1 implementation)

> Companion to `2026-06-24-chat-openrag-standalone-design.md` (the design spec,
> whose "Path to v2" section is the starting point). This doc consolidates the
> concrete findings from building v1 that change or sharpen the v2 plan.

## Where v1 landed (recap)

A standalone React app (`chat-openrag/` at the openRAG repo root) that:
- reuses `cozy-search`'s decoupled chat-thread components (`Conversation`,
  `ConversationComposer`, `AssistantMessage`, `UserMessage`, presentational
  `Sources`) + the two injection seams (`ConversationStore`, `ChatComponents`),
  consumed via `cozy-search/decoupled` (a new entry that avoids the eager-CJS
  barrel pulling cozy-client/realtime);
- talks to openRAG only over the OpenAI-compatible HTTP API
  (`OPENRAG_BASE_URL` + bearer token), via `SSE OpenRagChatAdapter`;
- persists conversations client-side in **IndexedDB**;
- runs its own providers (CozyTheme light + twake-i18n + cozy-ui CSS +
  BreakpointsProvider), with an app-own sidebar.

**The single defining constraint of v1:** the app has **no cozy-stack access**.
It authenticates to openRAG, not to any Cozy instance. Almost every "deferred to
v2" item below is downstream of closing that one gap.

## v2 goal

Embed `chat-openrag` via iframe into a thin Cozy shell inside Twake, with silent
SSO and a capability bridge for Cozy-native actions — as described in the design
spec. Once the app has a cozy-stack session, several v1 best-efforts become the
real thing.

---

## Work items

Each item: **what**, **why**, **v1 groundwork already in place**, **v2 approach**.

### A. openRAG-side auth gaps (blocking for silent SSO in an iframe)

From the design spec's audit (re-verify file:line before acting — the repo moves):
1. **Cookie `SameSite=Lax` → `None; Secure` (+ `Partitioned`/CHIPS).** A `Lax`
   cookie isn't sent in a cross-site iframe, so the session never sticks.
   Spec located these at `openrag/api/routers/auth/oidc.py` (state + session
   cookie). *Blocking.*
2. **No `prompt=none` (silent SSO).** `build_authorization_url` in
   `openrag/services/auth/oidc_client.py` builds params without `prompt`. Add
   `prompt=none` + handle `login_required`/`interaction_required` → popup
   fallback. *Blocking for the silent scenario.*
3. **No `Content-Security-Policy: frame-ancestors`.** Set it explicitly to the
   Twake/Cozy origin; verify no prod proxy injects a blocking `X-Frame-Options`.
   *Recommended.*

CORS is largely fine (`api/main.py` sets `allow_credentials=True` + configurable
`allow_origins`); chat→API is same-origin once the app is served by openRAG.

### B. Embedding shell + bridge

Reuse the existing `cozy-external-bridge-container` (parent) /
`cozy-external-bridge` (iframe) packages — Comlink over postMessage,
origin-pinned via the `requestParentOrigin`/`answerParentOrigin` handshake.
Mirrors Twake Mail / La Suite Visio: the Cozy shell authenticates to the stack
normally, frames the chat URL (from a feature flag), passes **no token** across
the boundary. The chat runs its own session on its own origin.

### C. Cozy-stack access in the app (the linchpin)

Once silent SSO (A) + the shell (B) are in place, the app can hold a
cozy-client / stack token. This unlocks D, E, F below. Decision point: does the
chat get its OWN cozy-client (authenticated via its session) or proxy stack
calls through the Comlink bridge to the shell's `client`? The bridge already
exposes scoped methods (`getContacts`, `search`, `getFlag`, …) — extending it is
the lighter path for occasional calls; a real cozy-client is better if the chat
needs broad stack access.

### D. Twake source links (the one we explicitly deferred)

**What:** clicking a source that is a Cozy file opens it in that instance's
Drive, exactly like cozy-search.

**Why v1 couldn't:** cozy-search resolves the link by querying the stack —
`source.id → Q('io.cozy.files').getByIds([id]) → file doc { dir_id, path } →
generateWebLink({slug:'drive', cozyUrl, subDomainType, hash:'/folder/<dir_id>/file/<_id>'})`.
It needs `dir_id`, which openRAG does **not** return (only `file_id`), and a
cozy-client (which v1 lacks).

**v1 groundwork (already wired):** `OpenRagSources` already detects Twake files
(`doctype === 'io.cozy.files' && file_id && partition`) and tags them
`kind: 'twake'`; `normalizeSources` carries `doctype`, `fileId`, `partition`.
v1 falls back to best-effort chunk content for every document.

**v2 approach:** with a cozy-client (C), for `kind:'twake'` sources do
`getByIds([fileId])` against the file's instance (`partition`) to fetch the file
doc, then build the link with `generateWebLink` — the exact cozy-search path.
(Alternative that avoids stack calls at click time: have the ingestion connector
include `dir_id` in openRAG `extra_metadata` so the source carries it directly.)

### E. Conversation persistence: IndexedDB → openRAG/stack

**What:** move conversation history off the browser into openRAG (or the stack)
so it follows the user across devices.

**v1 groundwork:** the `ConversationStore` seam is the clean abstraction. v1
provides `LocalConversationStore` (IndexedDB). v2 just provides a different
implementation of the same interface (`useConversations`, `useConversationMessages`,
create/delete/rename, `appendMessages`) backed by an openRAG/stack API. No view
changes. NB: openRAG is currently **stateless** (no `conversation_id`); v2 needs
an openRAG-side conversation store, or use the cozy-stack doctype
(`io.cozy.ai.chat.conversations`, as cozy-search does) via the cozy-client.

### F. Theming handoff

**What:** match the Twake instance accent color + dark mode.

**v1 groundwork:** single fixed cozy-ui light theme via `CozyTheme type="light"
ignoreCozySettings` (no stack). v2: URL param at load for first paint + Comlink
`getTheme` + live `onThemeChange`; drop `ignoreCozySettings` so CozyTheme reads
the real instance settings via the stack.

### G. cozy-search consumption

- **Publish** the decoupled `cozy-search` (branch `feat/decouple-chat-views`)
  and replace the v1 `file:` link with a pinned `^version`.
- With stack access (C), the app *could* switch from its app-own sidebar back to
  cozy-search's full `Sidebar` + `ConversationActions` (rename/delete/share),
  which need cozy-client — these were intentionally hidden in v1 (`disableAction`).

### H. Repo split (recommended, see v1 discussion)

Move `chat-openrag/` to a dedicated repo at the v1-merge boundary: clean HTTP
contract to openRAG, incompatible toolchains (Python vs JS), frontend trajectory
(belongs with the cozy/Twake frontend ecosystem). Use `git subtree split` to
keep history. Do it after publishing cozy-search (G).

### I. Build/integration carry-overs

v1 accumulated cozy-ecosystem build glue that v2 inherits (and can partly
simplify once it's a normal cozy-frontend app with a published cozy-search):
- rspack: `process/browser` shim, react/react-dom/@assistant-ui/react aliases
  (avoid dual-React), `cozy-ui`/`twake-i18n` aliases, stylus `paths` for cozy-ui
  `@require`, importing `cozy-search/dist/stylesheet.css` for component CSS.
- `@assistant-ui/react` pinned to **0.12.5** to match cozy-search (0.12.28 had
  an incompatible internal client/transform-scopes arch).
- jest: custom env bridging Node `fetch`/streams/`structuredClone` into jsdom;
  blanket cozy-ui stubs (component tests validate wiring, not real cozy-ui
  rendering — real rendering is covered by the rspack build + manual E2E).
- `cozy-intent` installed only because cozy-ui `Dialog` requires it (v1 no-op).

## Known v1 limitations that v2 should resolve

- Source `file_url` (`/static`) is unusable in practice: needs `?token=` AND the
  original file must be retained in the server `DATA_DIR` (staging 404s). v2's
  Twake links (D) sidestep this for Cozy files.
- Per-conversation rename/delete/share hidden (need cozy-client) — see G.
- Sidebar items show no assistant avatar (✦) because local conversations have no
  `assistant` relationship — a stack-backed store (E) would.

## References

- Design spec: `docs/superpowers/specs/2026-06-24-chat-openrag-standalone-design.md`
- Plans: `docs/superpowers/plans/2026-06-25-cozy-search-decoupling.md`,
  `docs/superpowers/plans/2026-06-25-chat-openrag-app.md`
- v1 implementation ledgers (gitignored scratch, may not persist):
  `.superpowers/sdd/progress.md`, `.superpowers/sdd/progress-plan2.md`
- cozy bridge: `cozy-libs/packages/cozy-external-bridge[-container]`
- cozy-search link mechanism: `FileSourcesItem.jsx` + `CozySourcesWithFilesQuery.jsx`
  (`getByIds` → `generateWebLink`)
