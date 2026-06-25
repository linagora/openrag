/**
 * Custom jest environment: extends jest-environment-jsdom and
 * copies Node 18+ native fetch globals into the jsdom window.
 * This lets tests spy on global.fetch without extra polyfills.
 */
const { TestEnvironment } = require('jest-environment-jsdom')

class FetchJsdomEnvironment extends TestEnvironment {
  async setup() {
    await super.setup()
    // Node 18+ exposes fetch on the Node global; copy into jsdom global.
    if (typeof globalThis.fetch === 'function') {
      this.global.fetch = globalThis.fetch
      this.global.Headers = globalThis.Headers
      this.global.Request = globalThis.Request
      this.global.Response = globalThis.Response
    }
    // Also copy TextEncoder, TextDecoder, and ReadableStream
    if (typeof globalThis.TextEncoder === 'function') {
      this.global.TextEncoder = globalThis.TextEncoder
    }
    if (typeof globalThis.TextDecoder === 'function') {
      this.global.TextDecoder = globalThis.TextDecoder
    }
    if (typeof globalThis.ReadableStream === 'function') {
      this.global.ReadableStream = globalThis.ReadableStream
    }
  }
}

module.exports = FetchJsdomEnvironment
