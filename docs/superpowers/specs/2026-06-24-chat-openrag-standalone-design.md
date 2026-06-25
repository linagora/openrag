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

**Sources display:** same in-chat rendering as `cozy-search` today. When a
source has a link, link to it. Showing the source content inside the app (PDF,
TXT, etc.) when no external link is defined is **deferred to later** (post-v1).

### Model / partition selection

`GET /v1/models` lists the partitions the user's token grants access to. This
feeds a selector reusing the "assistant selection" UI slot (without the Cozy
logic). **Default partition: the first one returned** when the user has access
to several (no hardcoded default).

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
   one (no Cozy `doctype`). The adapter normalizes it so the `Sources` component
   renders sources the same way `cozy-search` does today, linking to the source
   when a link is defined. In-app source content viewing (PDF/TXT/…) when no
   external link exists is deferred (post-v1).
2. **MUI v4 standalone via rspack** — to validate (expected fine with
   `babel-preset-cozy-app`).
3. **`twake-i18n` standalone setup** — provide locales without the Cozy app
   context.
4. **`cozy-search` decoupling** — the real refactor effort: identify every point
   in the views touching `cozy-client` / `cozy-realtime`.
5. **Cross-repo dev loop (resolved)** — `chat-openrag` is developed against a
   locally linked, not-yet-published decoupled `cozy-search`
   (`yarn link` or `file:` dependency) until a version exposing the decoupled
   views is published. Standard approach, accepted.

## Path to v2 (informational, not part of this spec)

v2 embeds `chat-openrag` via iframe into a Cozy "shell" app within Twake, with
silent SSO and a capability bridge for Cozy-native actions. The two topics below
were researched and validated; they are recorded here so the v2 effort starts
from facts, not assumptions.

### Embedding & shell

- Embed `chat-openrag` via iframe into a thin Cozy shell app, reusing the
  existing `cozy-external-bridge-container` (parent) / `cozy-external-bridge`
  (iframe) packages — Comlink RPC over postMessage, origin-pinned via the
  `requestParentOrigin` / `answerParentOrigin` handshake.
- This mirrors Twake Mail / La Suite Visio exactly: the Cozy shell authenticates
  to the stack normally, frames an arbitrary URL (from a feature flag), and
  passes **no token** across the boundary. The embedded app runs its own session
  on its own origin.

### Authentication — silent SSO (shared IdP)

Decided: openRAG and Twake share the **same IdP**; the chat authenticates via a
**silent SSO session** (`prompt=none`) inside the iframe. The Cozy side imposes
no auth contract — all the work is on openRAG's side. Audit of the openRAG repo
(`/home/paul/dev/linagora/server/openrag`) found **no structural blocker** but
**three concrete gaps to close**:

1. **Cookie `SameSite=Lax` → must become `None; Secure`** *(blocking)*.
   `openrag/api/routers/auth/oidc.py:104` (state cookie) and `:140` (session
   cookie) hardcode `samesite="lax"`. A `Lax` cookie is not sent in a cross-site
   iframe, so the session would never stick. Needs `SameSite=None; Secure`, ideally
   plus `Partitioned` (CHIPS) for Chrome's third-party-cookie phase-out. The
   `Secure` flag is already conditionally correct behind a properly-configured
   proxy (`proxy_headers` + `forwarded_allow_ips`, see `api/main.py:367-381`).
2. **No `prompt=none` (silent SSO) support** *(blocking for the silent scenario)*.
   `openrag/services/auth/oidc_client.py` `build_authorization_url` builds the
   authorization params **without `prompt`**. Add `prompt=none` and handle the
   IdP's `login_required` / `interaction_required` error response → fall back to
   a popup/new-tab interactive login.
3. **No `Content-Security-Policy: frame-ancestors`** *(recommended)*. openRAG
   emits no CSP and no `X-Frame-Options` (so framing works by default today), but
   v2 should set `frame-ancestors <twake/cozy origin>` explicitly and verify no
   production proxy injects a blocking `X-Frame-Options`.

CORS is largely fine: `api/main.py:277` sets `allow_credentials=True` with a
configurable `allow_origins`, and since the chat is **served by openRAG** (same
origin as its API), chat→API calls are same-origin.

Target silent flow once the gaps are closed: the shell (or the chat) loads
openRAG `/auth/login?prompt=none` in a hidden iframe → the user already has an
IdP session (entered Twake via SSO) → the IdP redirects silently to
`/auth/callback` → session cookie set → chat authenticated with nothing shown.
No IdP session → `login_required` → popup fallback. A full-page interactive OIDC
redirect inside the iframe will **not** work (IdPs set `X-Frame-Options: DENY`).

### Cozy-native actions — intents via parent-broker

Need: trigger Cozy-native actions from the chat (e.g. a Drive file picker via a
`PICK io.cozy.files` intent). Researched against the stack intents doc and
`cozy-interapp` / `cozy-ui-plus` source. Conclusions:

- **The chat iframe cannot start a Cozy intent directly.** Starting an intent
  requires `POST /intents` via an authenticated `cozy-client` (stack token) and a
  postMessage handshake that validates Cozy origins. The chat is on the openRAG
  origin with no token, so it fails on both counts. Confirmed.
- **No iframe-in-iframe.** Although stack intents do support nesting (`compose`),
  the correct pattern here is the **parent-broker**: the chat calls a bridge
  method (e.g. `pickFile()` / `startIntent(action, doctype, data)`); the Cozy
  shell — which holds `cozy-client` and runs on the Cozy origin — is the real
  intent client. It runs `client.intents.create('PICK','io.cozy.files').start(el)`,
  injecting the **Drive service iframe into the PARENT's DOM** (a host node /
  modal overlay in the shell, exactly like `cozy-ui-plus`'s `IntentIframe`), then
  returns the picked `io.cozy.files` document (plain JSON) to the chat over the
  bridge. The shell hosts two sibling iframes (chat + transient intent service),
  never nested.
- **Consistent with the existing bridge.** `cozy-external-bridge-container`
  already exposes scoped methods (`getContacts`, `createDocs`, `updateDocs`,
  `search`, `getFlag`, …) over Comlink and holds `client` on the Cozy origin.
  Adding `pickFile()` / `startIntent(...)` is the natural extension; the child
  side's `CozyBridge.availableMethods` type list must be extended too. To handle:
  render an intent-host DOM node in the shell and manage its modal
  visibility/lifecycle (the `start()` Promise injects/removes the service iframe).
- Note: the relevant intent system is **stack intents** (`cozy-interapp` +
  `cozy-ui-plus/Intent`), NOT the `cozy-intent` package (a React-Native↔webview
  bridge, unrelated).

### Remaining v2 items

- Add a theming handoff (URL param at load for first paint + Comlink `getTheme`
  + live `onThemeChange`) to match instance accent color / dark mode.
- Migrate conversation persistence from IndexedDB to openRAG.
- Retire the chat from `cozy-search` (consolidation).
```
