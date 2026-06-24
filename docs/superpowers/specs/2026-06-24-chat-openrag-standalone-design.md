# Design — `chat-openrag` standalone chat app (v1)

**Date:** 2026-06-24
**Status:** Design approved, pending spec review
**Author:** Paul Tran-Van (brainstormed with Claude)

## Context & motivation

Today the Cozy AI assistant lives in the `cozy-search` package (`cozy/cozy-libs`):
a React UI built on `@assistant-ui/react`, talking to the Cozy stack
(`POST /ai/chat/conversations/{id}` + `cozy-realtime` WebSocket), shipped as a
library embedded into each Cozy app.

In parallel, openRAG (the RAG backend used by the chat) has no UI of its own —
it exposes the RAG API. We want **openRAG to carry its own chat**, to avoid
maintaining two competing chat front-ends (openRAG's and `cozy-search`'s) and to
let openRAG be a self-contained product, independent of Twake/Cozy.

**Target vision (not v1):** openRAG hosts a self-contained chat app (UI +
conversation storage + streaming + RAG), embedded via iframe into Cozy apps
through SSO (the Twake Mail / La Suite Visio pattern), with the chat eventually
removed from `cozy-search`. Conversations and history migrate to openRAG
(authentication via openRAG's OIDC/SSO mode).

**This document covers v1 only.**

## v1 goal

Extract the `cozy-search` chat UI into a **standalone, deployable React app**
(`chat-openrag`) that:

- runs on its own (not yet embedded in an iframe),
- uses a single fixed cozy-ui theme,
- talks to openRAG's existing OpenAI-compatible chat API,
- persists conversation history client-side (IndexedDB).

v1 de-risks the UI and the openRAG data layer before the iframe/SSO integration
(v2).

## Approach (decided)

**Approach 2 — decouple presentational views from the runtime/data layer.**

`cozy-search` is split by responsibility, exploiting two injection seams. The new
app provides its own runtime (openRAG flavor) and reuses `cozy-search`'s
presentational views as a published npm dependency.

```
┌──────────────────────────────────────────────────────────────┐
│  chat-openrag  (NEW — React app, lives in openRAG repo root)   │
│                                                                │
│  • Standalone providers: CozyTheme(light) + I18n + cozy-ui CSS │
│  • OpenRagChatAdapter        → SSE POST /v1/chat/completions    │
│  • LocalConversationStore    → IndexedDB (history)             │
│  • Model selection           → GET /v1/models (partitions)     │
│  • Config: openRAG baseURL + bearer token (dev)                │
└───────────────┬────────────────────────────────────────────────┘
                │ imports views, injects adapter + store
                ▼
┌──────────────────────────────────────────────────────────────┐
│  cozy-search  (EXISTING — becomes the view provider)           │
│                                                                │
│  VIEW layer (presentational, no cozy-client):                  │
│   Conversation · Messages · Composer · ConversationList ·      │
│   Sources · Sidebar → consume @assistant-ui + cozy-ui + props  │
│                                                                │
│  RUNTIME layer (Cozy flavor, untouched):                       │
│   CozyRealtimeChatAdapter · stack-backed store · realtime      │
└──────────────────────────────────────────────────────────────┘
```

### Two injection seams

1. **`ChatModelAdapter`** (`@assistant-ui/react` interface) — the seam
   **already exists**. `cozy-search` keeps `CozyRealtimeChatAdapter`; the app
   provides `OpenRagChatAdapter`. No view changes required.
2. **`ConversationStore`** (new interface to introduce) — abstracts conversation
   listing/CRUD (today `useFetchConversations` + `cozy-client`). `cozy-search`
   keeps a stack-backed implementation; the app provides `LocalConversationStore`
   (IndexedDB).

### Decoupling work in `cozy-search`

- Guarantee views **never import `cozy-client` / `cozy-realtime`** directly —
  data flows through the assistant-ui runtime + props/callbacks.
- Extract the `ConversationStore` interface and route
  `useFetchConversations` / `useConversation` behind it.
- Assistant selection and the `TwakeKnowledges` panel (Cozy-data-coupled) are
  **out of scope for v1** (stubbed/hidden in the app).

## Build & tooling

- **Stack:** React (reuses `cozy-search` views; React coexists with whatever
  else is in the repo — the existing openRAG frontend is being retired and is
  not a constraint).
- **Bundler:** **rspack + `babel-preset-cozy-app`**, config derived from
  `cozy-libs/packages/cozy-external-bridge/rspack.config.mjs`. This is the safe
  choice to guarantee that cozy-ui / cozy-search build correctly (MUI v4, Stylus
  `.styl`, inline SVG via `babel-plugin-inline-react-svg`, module-resolver — the
  exact Cozy toolchain). It is also the build setup the v2 iframe app will need,
  so nothing is thrown away.
- **Location:** `chat-openrag/` at the openRAG repo root (for now).
- **`cozy-search` consumption:** as a published npm package. During development,
  use a local link (`yarn link` / file path) against the decoupled build until a
  version exposing the decoupled views is published.

## Data flow (v1 standalone)

```
boot → providers (CozyTheme light + I18n + cozy-ui CSS)
     → OpenRagChatRuntimeProvider
        (assistant-ui runtime + OpenRagChatAdapter + LocalConversationStore)

1. Sidebar: conversation list  ← LocalConversationStore (IndexedDB)
2. Select / create conversation → messages loaded from store
3. Send message → runtime calls OpenRagChatAdapter.run({ messages })
       POST /v1/chat/completions
       { model: <partition>, messages: [...full history], stream: true }
4. SSE: chunks `data:{…delta.content…}` → progressive yield → UI streams text
5. Final chunk (finish_reason:"stop") → parse `extra.sources` → normalize
       → metadata.custom.sources → Sources component renders; ends on `data:[DONE]`
6. Persist (user message + assistant message + sources) to LocalConversationStore
```

### `OpenRagChatAdapter`

Implements `ChatModelAdapter`. Replaces `StreamBridge` + WebSocket with an **SSE
reader** (`fetch` + `ReadableStream`). Normalizes openRAG sources
(`source_type: document | web`, with `file_url` / `chunk_url` / `url` / `title` /
`snippet`) into the shape the `Sources` component expects.

openRAG is **stateless**: no `conversation_id`. The adapter sends the **full
`messages[]` array** on every request (openRAG keeps only the last N per
`chat_history_depth`).

### Model / partition selection

`GET /v1/models` lists partitions (`openrag-<partition>` / `openrag-all`). This
feeds a selector reusing the "assistant selection" UI slot (without the Cozy
logic). **Default partition: `openrag-all`** (to confirm).

## Theming (v1)

Single fixed cozy-ui theme. cozy-ui ships its own CSS variables
(`stylus/settings/themes/light.styl` defines `--primaryColor` etc. on `html`
from `theme/palette.json`), so importing the cozy-ui stylesheet yields the
default Cozy/Twake theme without a stack. Setup:

- import the cozy-ui stylesheet (CSS variables + `u-*` utilities),
- wrap in `CozyTheme type="light"` (cozy-ui MUI v4 theme provider),
- provide an I18n provider (`twake-i18n`),
- ship the Lato font,
- provide `cozy-intent` as a no-op if a component requires it.

No theming bridge. The URL-param / Comlink `getTheme` mechanism and dark mode are
deferred to v2.

## Auth (v1)

openRAG `baseURL` + **bearer token** via app config/env (dev). openRAG's
OIDC/SSO mode already exists and is wired in v2.

## Out of scope for v1 (non-goals)

- ❌ iframe embedding + Comlink bridge (v2)
- ❌ SSO / OIDC (v1 = dev token)
- ❌ Theming bridge (URL param / `getTheme`), dark-mode toggle
- ❌ `TwakeKnowledges` Cozy-data panel (hidden/stubbed)
- ❌ Removing chat from `cozy-search` and host apps (= later consolidation)
- ❌ openRAG-side conversation persistence (IndexedDB in v1)

## Risks & open questions

1. **Source normalization** — openRAG's source shape differs from the current
   one (no Cozy `doctype`). The `Sources` component may need a small display
   variant (document vs web).
2. **MUI v4 standalone via rspack** — to validate (expected fine with
   `babel-preset-cozy-app`).
3. **`twake-i18n` standalone setup** — provide locales without the Cozy app
   context.
4. **`cozy-search` decoupling** — the real refactor effort: identify every point
   in the views touching `cozy-client` / `cozy-realtime`.
5. **Default partition & `/v1/models` mapping** — confirm partition ↔ "assistant"
   semantics and the default selection.
6. **Cross-repo dev loop** — developing `chat-openrag` against a not-yet-published
   decoupled `cozy-search`; use local linking until published.

## Path to v2 (informational, not part of this spec)

- Embed `chat-openrag` via iframe into Cozy apps using the existing
  `cozy-external-bridge-container` / `cozy-external-bridge` (Comlink over
  postMessage, origin-pinned).
- Authenticate the user via openRAG's OIDC/SSO mode (Twake Mail / Visio pattern):
  the embedded app does its own SSO login; the Cozy host can expose scoped Cozy
  capabilities over the bridge if needed (capability RPC, never a token).
- Add a theming handoff (URL param at load for first paint + Comlink `getTheme`
  + live `onThemeChange`) to match instance accent color / dark mode.
- Migrate conversation persistence from IndexedDB to openRAG.
- Retire the chat from `cozy-search` (consolidation).
```
