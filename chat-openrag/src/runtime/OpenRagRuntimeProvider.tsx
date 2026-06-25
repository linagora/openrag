import {
  AssistantRuntimeProvider,
  useLocalRuntime
} from '@assistant-ui/react'
import type { ThreadMessageLike } from '@assistant-ui/react'
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

const OpenRagRuntimeProviderInner = ({
  store: _store,
  initialMessages,
  children
}: InnerProps): JSX.Element => {
  const { model } = useModel()
  const adapter = useMemo(() => createOpenRagChatAdapter({ model }), [model])
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
