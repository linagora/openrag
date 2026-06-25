import React from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { useConversationStore } from 'cozy-search/decoupled'
import type { ConversationSummary } from 'cozy-search/decoupled'

const conversationTitle = (c: ConversationSummary): string => {
  if (c.name) return c.name
  const firstUser = (c.messages || []).find(m => m.role === 'user')
  const text = firstUser?.content?.trim()
  return text ? text.slice(0, 60) : 'New conversation'
}

const AppSidebar = (): JSX.Element => {
  const navigate = useNavigate()
  const { conversationId } = useParams<{ conversationId: string }>()
  const store = useConversationStore()
  const { conversations } = store.useConversations()

  const onNew = async (): Promise<void> => {
    const id = await store.createConversation()
    navigate(`/assistant/${id}`)
  }

  return (
    <nav className="u-flex u-flex-column u-p-1 u-bdr-right" aria-label="Conversations">
      <button type="button" className="u-mb-1" onClick={onNew}>
        New conversation
      </button>
      <ul className="u-list-unstyled u-m-0 u-p-0">
        {conversations.map(c => (
          <li key={c._id}>
            <button
              type="button"
              aria-current={c._id === conversationId ? 'page' : undefined}
              className="u-w-100 u-ta-left"
              onClick={() => navigate(`/assistant/${c._id}`)}
            >
              {conversationTitle(c)}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  )
}

export default AppSidebar
