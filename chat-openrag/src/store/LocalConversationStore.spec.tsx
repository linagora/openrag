import { localDb } from './localDb'

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
