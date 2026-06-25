import { openDB, IDBPDatabase } from 'idb'

import type { ConversationSummary, StoredMessage } from 'cozy-search'

const DB_NAME = 'chat-openrag'
const STORE = 'conversations'

let dbPromise: Promise<IDBPDatabase> | null = null
const db = (): Promise<IDBPDatabase> => {
  if (!dbPromise) {
    dbPromise = openDB(DB_NAME, 1, {
      upgrade(database) {
        if (!database.objectStoreNames.contains(STORE)) {
          database.createObjectStore(STORE, { keyPath: '_id' })
        }
      }
    })
  }
  return dbPromise
}

const touch = (c: ConversationSummary): ConversationSummary => ({
  ...c,
  cozyMetadata: { updatedAt: new Date().toISOString() }
})

export const localDb = {
  async listConversations(): Promise<ConversationSummary[]> {
    const all = (await (await db()).getAll(STORE)) as ConversationSummary[]
    return all.sort((a, b) =>
      (b.cozyMetadata?.updatedAt || '').localeCompare(a.cozyMetadata?.updatedAt || '')
    )
  },
  async getConversation(id: string): Promise<ConversationSummary | undefined> {
    return (await db()).get(STORE, id) as Promise<ConversationSummary | undefined>
  },
  async putConversation(c: ConversationSummary): Promise<void> {
    await (await db()).put(STORE, touch({ messages: [], ...c }))
  },
  async appendMessages(id: string, msgs: StoredMessage[]): Promise<void> {
    const existing =
      (await this.getConversation(id)) || ({ _id: id, messages: [] } as ConversationSummary)
    await (await db()).put(
      STORE,
      touch({ ...existing, messages: [...(existing.messages || []), ...msgs] })
    )
  },
  async deleteConversation(id: string): Promise<void> {
    await (await db()).delete(STORE, id)
  }
}
