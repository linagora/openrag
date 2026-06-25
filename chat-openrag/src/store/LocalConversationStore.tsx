import { useCallback, useEffect, useMemo, useState } from 'react'

import type {
  ConversationStore,
  ConversationSummary,
  StoredMessage
} from 'cozy-search'

import { localDb } from './localDb'

export interface LocalStore extends ConversationStore {
  appendMessages: (id: string, msgs: StoredMessage[]) => Promise<void>
}

// Module-level version bumped on every write so all useConversations()
// consumers re-read after a mutation (no realtime backend here).
let version = 0
const listeners = new Set<() => void>()
const bump = (): void => {
  version++
  listeners.forEach(l => l())
}

export const useLocalConversationStore = (): LocalStore => {
  const useConversations: ConversationStore['useConversations'] = () => {
    const [conversations, setConversations] = useState<ConversationSummary[]>([])
    const [isLoading, setLoading] = useState(true)
    const reload = useCallback(() => {
      localDb.listConversations().then(c => {
        setConversations(c)
        setLoading(false)
      })
    }, [])
    useEffect(() => {
      reload()
      listeners.add(reload)
      return () => {
        listeners.delete(reload)
      }
    }, [reload])
    return { conversations, hasMore: false, isLoading, fetchMore: () => {} }
  }

  const useConversationMessages: ConversationStore['useConversationMessages'] = (
    conversationId: string
  ) => {
    const [messages, setMessages] = useState<StoredMessage[]>([])
    const [isLoading, setLoading] = useState(true)
    useEffect(() => {
      let active = true
      localDb.getConversation(conversationId).then(c => {
        if (!active) return
        setMessages(c?.messages || [])
        setLoading(false)
      })
      return () => {
        active = false
      }
    }, [conversationId])
    return { messages, isLoading }
  }

  const createConversation = useCallback(async () => {
    const id = `conv-${version}-${Date.now()}`
    await localDb.putConversation({ _id: id })
    bump()
    return id
  }, [])

  const deleteConversation = useCallback(async (id: string) => {
    await localDb.deleteConversation(id)
    bump()
  }, [])

  const renameConversation = useCallback(async (id: string, name: string) => {
    const existing = await localDb.getConversation(id)
    await localDb.putConversation({ _id: id, ...existing, name })
    bump()
  }, [])

  const appendMessages = useCallback(async (id: string, msgs: StoredMessage[]) => {
    await localDb.appendMessages(id, msgs)
    bump()
  }, [])

  return useMemo(
    () => ({
      useConversations,
      useConversationMessages,
      createConversation,
      deleteConversation,
      renameConversation,
      appendMessages
    }),
    [createConversation, deleteConversation, renameConversation, appendMessages]
  )
}
