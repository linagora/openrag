import { useCallback, useEffect, useState } from 'react'

import type {
  ConversationStore,
  ConversationSummary,
  StoredMessage
} from 'cozy-search'

import { localDb } from './localDb'

export interface LocalStore extends ConversationStore {
  appendMessages: (id: string, msgs: StoredMessage[]) => Promise<void>
}

let version = 0
let idCounter = 0
const listeners = new Set<() => void>()
const bump = (): void => {
  version++
  listeners.forEach(l => l())
}

function useConversations(): ReturnType<ConversationStore['useConversations']> {
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [isLoading, setLoading] = useState(true)
  useEffect(() => {
    let active = true
    const reload = (): void => {
      localDb.listConversations().then(c => {
        if (!active) return
        setConversations(c)
        setLoading(false)
      })
    }
    reload()
    listeners.add(reload)
    return () => {
      active = false
      listeners.delete(reload)
    }
  }, [])
  return { conversations, hasMore: false, isLoading, fetchMore: () => {} }
}

function useConversationMessages(
  conversationId: string
): ReturnType<ConversationStore['useConversationMessages']> {
  // Track which conversation the loaded messages belong to. Deriving isLoading
  // from `loaded.id !== conversationId` makes the switch synchronous: the
  // instant conversationId changes, isLoading is true again (before the async
  // load), so a consumer that gates on it never seeds with the previous
  // conversation's messages.
  const [loaded, setLoaded] = useState<{
    id: string | null
    messages: StoredMessage[]
  }>({ id: null, messages: [] })

  useEffect(() => {
    let active = true
    const reload = (): void => {
      localDb.getConversation(conversationId).then(c => {
        if (active) setLoaded({ id: conversationId, messages: c?.messages || [] })
      })
    }
    reload()
    listeners.add(reload)
    return () => {
      active = false
      listeners.delete(reload)
    }
  }, [conversationId])

  const isLoading = loaded.id !== conversationId
  return { messages: isLoading ? [] : loaded.messages, isLoading }
}

export const useLocalConversationStore = (): LocalStore => {
  const createConversation = useCallback(async () => {
    const id = `conv-${Date.now()}-${idCounter++}`
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

  return {
    useConversations,
    useConversationMessages,
    createConversation,
    deleteConversation,
    renameConversation,
    appendMessages
  }
}
