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

export const OpenRagRuntimeProvider = ({
  conversationId,
  store,
  children
}: {
  conversationId: string
  store: LocalStore
  children: ReactNode
}): JSX.Element => {
  const { model } = useModel()
  const { messages } = store.useConversationMessages(conversationId)
  const initialMessages = useMemo(() => toThreadMessages(messages), [messages])

  const adapter = useMemo(() => createOpenRagChatAdapter({ model }), [model])

  const runtime = useLocalRuntime(adapter, { initialMessages })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      {children}
    </AssistantRuntimeProvider>
  )
}
