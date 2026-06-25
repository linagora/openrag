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
  const [messages, setMessages] = useState<StoredMessage[]>([])
  const [isLoading, setLoading] = useState(true)
  useEffect(() => {
    let active = true
    const reload = (): void => {
      localDb.getConversation(conversationId).then(c => {
        if (!active) return
        setMessages(c?.messages || [])
        setLoading(false)
      })
    }
    reload()
    listeners.add(reload)
    return () => {
      active = false
      listeners.delete(reload)
    }
  }, [conversationId])
  return { messages, isLoading }
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
