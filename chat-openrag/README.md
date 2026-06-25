# chat-openrag

Standalone chat app for OpenRAG, built with React + assistant-ui + cozy-search.

## Dev setup

`cozy-search` is consumed via a local `file:` link. Before running `npm install`
here, build the package in the other repo:

```bash
# In /home/paul/dev/cozy/apps/cozy-libs (branch feat/decouple-chat-views)
cd packages/cozy-search
yarn build
```

Then install and start:

```bash
npm install
npm run dev     # http://localhost:3042
```

The `cozy-search` entry used is `dist/decoupled.js` (the decoupled build that
does NOT pull in `cozy-client` directly). The rspack config pins several
`cozy-search` peer dependencies (e.g. `@linagora/twake-icons`,
`cozy-device-helper`) back to this app's own `node_modules` to avoid
duplicates across the symlink boundary.

## Before CI / production

The `file:` path in `package.json` is machine-specific and will break any
`npm install` outside this machine:

```json
"cozy-search": "file:/home/paul/dev/cozy/apps/cozy-libs/packages/cozy-search"
```

Before deploying or running in CI:

1. Publish the `cozy-search` package to the npm registry (or a private
   registry) and pin the version that includes the `decoupled` entry point
   (i.e. after the `feat/decouple-chat-views` branch is merged and released).
2. Replace the `file:` entry with the published version:
   ```json
   "cozy-search": "^X.Y.Z"
   ```
3. Keep the following app-level rspack `resolve.alias` entries that bridge the
   linked monorepo package — they remain necessary when `cozy-search` is
   installed from the registry because `cozy-ui` and `cozy-search` must share
   the same singleton instances of these packages:
   - `react` / `react-dom`
   - `@assistant-ui/react`
   - `twake-i18n`
   - `cozy-ui` / `cozy-ui-plus`
   - `@linagora/twake-icons`
   - `cozy-device-helper`
