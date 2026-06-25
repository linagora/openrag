import type {
  ChatModelAdapter,
  ChatModelRunOptions,
  ChatModelRunResult
} from '@assistant-ui/react'

import type { StoredMessage } from 'cozy-search'

import type { LocalStore } from '../store/LocalConversationStore'

const textOf = (
  parts: readonly { type: string }[] | undefined
): string =>
  ((parts || []) as readonly { type: string }[])
    .filter((p): p is { type: 'text'; text: string } => p.type === 'text')
    .map(p => p.text)
    .join('')

/**
 * Wraps a base ChatModelAdapter so that each completed turn is persisted to
 * the store. Exactly two messages are appended per turn: the latest user
 * message and the new assistant message (with optional sources). Prior history
 * already lives in the store, so there is no duplication.
 */
export function createPersistingAdapter(
  baseAdapter: ChatModelAdapter,
  store: LocalStore,
  conversationId: string
): ChatModelAdapter {
  return {
    async *run(opts: ChatModelRunOptions) {
      let last: ChatModelRunResult | undefined
      const result = baseAdapter.run(opts)
      if (Symbol.asyncIterator in result) {
        for await (const r of result) {
          last = r
          yield r
        }
      } else {
        last = await result
        yield last
      }
      if (last && last.status?.type === 'complete') {
        const lastUser = [...opts.messages]
          .reverse()
          .find(m => m.role === 'user')
        const userText = textOf(lastUser?.content)
        const assistantText = textOf(last.content)
        const sources = last.metadata?.custom?.sources as
          | StoredMessage['sources']
          | undefined
        const n = opts.messages.length
        await store.appendMessages(conversationId, [
          { id: `u-${n}-${Date.now()}`, role: 'user', content: userText },
          {
            id: `a-${n}-${Date.now()}`,
            role: 'assistant',
            content: assistantText,
            ...(sources ? { sources } : {})
          }
        ])
      }
    }
  }
}
