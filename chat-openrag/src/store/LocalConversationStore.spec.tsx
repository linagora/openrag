import { act, render } from '@testing-library/react'
import React, { useRef } from 'react'

import type { StoredMessage } from 'cozy-search'

import { localDb } from './localDb'
import { useLocalConversationStore } from './LocalConversationStore'

describe('localDb (IndexedDB)', () => {
  beforeEach(async () => {
    for (const c of await localDb.listConversations())
      await localDb.deleteConversation(c._id)
  })

  it('creates, lists, appends, and deletes', async () => {
    await localDb.putConversation({ _id: 'c1', name: 'First' })
    await localDb.appendMessages('c1', [
      { id: 'm1', role: 'user', content: 'hi' },
      { id: 'm2', role: 'assistant', content: 'hello', sources: [] }
    ])

    const list = await localDb.listConversations()
    expect(list.map(c => c._id)).toContain('c1')

    const conv = await localDb.getConversation('c1')
    expect(conv?.messages).toHaveLength(2)
    expect(conv?.messages?.[1].role).toBe('assistant')

    await localDb.deleteConversation('c1')
    expect(await localDb.getConversation('c1')).toBeUndefined()
  })
})

// Minimal renderHook compatible with @testing-library/react@12
function renderHook<T>(useHook: () => T): { readonly result: { current: T } } {
  const result = { current: null as unknown as T }
  const Wrapper = (): null => {
    result.current = useHook()
    return null
  }
  render(React.createElement(Wrapper))
  return { result }
}

describe('useLocalConversationStore', () => {
  beforeEach(async () => {
    for (const c of await localDb.listConversations())
      await localDb.deleteConversation(c._id)
  })

  it('useConversationMessages refreshes after appendMessages without remounting (Fix 2)', async () => {
    const storeRef = { current: null as ReturnType<typeof useLocalConversationStore> | null }
    const StoreCapture = (): null => {
      storeRef.current = useLocalConversationStore()
      return null
    }
    await act(async () => {
      render(React.createElement(StoreCapture))
    })
    const store = storeRef.current!

    // Create a conversation
    let convId: string = ''
    await act(async () => {
      convId = await store.createConversation()
    })

    // Render useConversationMessages for the created conversation
    const msgRef = { current: { messages: [] as StoredMessage[], isLoading: true } }
    const MessagesCapture = (): null => {
      msgRef.current = store.useConversationMessages(convId)
      return null
    }
    await act(async () => {
      render(React.createElement(MessagesCapture))
    })

    expect(msgRef.current.messages).toHaveLength(0)

    // Append a message — bump() should trigger listener → re-read in useConversationMessages
    await act(async () => {
      await store.appendMessages(convId, [
        { id: 'msg-1', role: 'user', content: 'hello from test' }
      ])
    })

    // Without Fix 2, useConversationMessages does not subscribe to listeners,
    // so messages would still be [] here. With Fix 2 it updates reactively.
    expect(msgRef.current.messages).toHaveLength(1)
    expect(msgRef.current.messages[0].content).toBe('hello from test')
  })
})
