import {
  AssistantRuntimeProvider,
  useLocalRuntime
} from '@assistant-ui/react'
import type {
  ChatModelAdapter,
  ChatModelRunResult,
  ThreadMessageLike
} from '@assistant-ui/react'
import React, { ReactNode, useMemo } from 'react'

import type { StoredMessage } from 'cozy-search'

import { createOpenRagChatAdapter } from '../openrag/OpenRagChatAdapter'
import type { LocalStore } from '../store/LocalConversationStore'
import { useModel } from './ModelContext'

const toThreadMessages = (messages: StoredMessage[]): ThreadMessageLike[] =>
  messages.map((m, i) => ({
    id: m.id || `msg-${i}`,
    role: m.role,
    content: m.content,
    metadata:
      m.role === 'assistant' && m.sources
        ? { custom: { sources: m.sources } }
        : undefined
  }))

interface InnerProps {
  conversationId: string
  store: LocalStore
  initialMessages: ThreadMessageLike[]
  children: ReactNode
}

const textOf = (
  parts: readonly { type: string }[] | undefined
): string =>
  ((parts || []) as readonly { type: string }[])
    .filter((p): p is { type: 'text'; text: string } => p.type === 'text')
    .map(p => p.text)
    .join('')

const OpenRagRuntimeProviderInner = ({
  conversationId,
  store,
  initialMessages,
  children
}: InnerProps): JSX.Element => {
  const { model } = useModel()
  const baseAdapter = useMemo(() => createOpenRagChatAdapter({ model }), [model])
  // Wrap the (reviewed, untouched) base adapter so that a completed turn is
  // persisted to the store. Each turn appends exactly the latest user message
  // plus the new assistant message; prior history already lives in the store
  // (from seeding + earlier turns), so there is no duplication.
  const adapter = useMemo<ChatModelAdapter>(
    () => ({
      async *run(opts) {
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
          const lastUser = [...opts.messages].reverse().find(m => m.role === 'user')
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
    }),
    [baseAdapter, store, conversationId]
  )
  const runtime = useLocalRuntime(adapter, { initialMessages })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      {children}
    </AssistantRuntimeProvider>
  )
}

export const OpenRagRuntimeProvider = ({
  conversationId,
  store,
  children
}: {
  conversationId: string
  store: LocalStore
  children: ReactNode
}): JSX.Element | null => {
  const { messages, isLoading } = store.useConversationMessages(conversationId)

  if (isLoading) return null

  return (
    <OpenRagRuntimeProviderInner
      key={conversationId}
      conversationId={conversationId}
      store={store}
      initialMessages={toThreadMessages(messages)}
    >
      {children}
    </OpenRagRuntimeProviderInner>
  )
}
