import React from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { useI18n } from 'twake-i18n'
import { useConversationStore } from 'cozy-search/decoupled'
import type { ConversationSummary } from 'cozy-search/decoupled'

const conversationTitle = (c: ConversationSummary, fallback: string): string => {
  if (c.name) return c.name
  const firstUser = (c.messages || []).find(m => m.role === 'user')
  const text = firstUser?.content?.trim()
  return text ? text.slice(0, 60) : fallback
}

// Last assistant reply (or any last message), trimmed, as a one-line preview.
const conversationPreview = (c: ConversationSummary): string => {
  const msgs = c.messages || []
  const last = [...msgs].reverse().find(m => m.role === 'assistant') || msgs[msgs.length - 1]
  return (last?.content || '').replace(/\s+/g, ' ').trim().slice(0, 70)
}

const formatDate = (iso: string | undefined, lang: string): string => {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return new Intl.DateTimeFormat(lang, { day: 'numeric', month: 'short' }).format(d)
}

const AppSidebar = (): JSX.Element => {
  const navigate = useNavigate()
  const { t, lang } = useI18n()
  const { conversationId } = useParams<{ conversationId: string }>()
  const store = useConversationStore()
  const { conversations } = store.useConversations()

  const newLabel = t('assistant.sidebar.create_new')

  const onNew = async (): Promise<void> => {
    const id = await store.createConversation()
    navigate(`/assistant/${id}`)
  }

  return (
    <nav className="app-sidebar" aria-label="Conversations">
      <button type="button" className="app-new-btn" onClick={onNew}>
        + {newLabel}
      </button>
      <div className="app-sidebar-header">
        {t('assistant.sidebar.recent_chats')}
      </div>
      <ul className="app-sidebar-list">
        {conversations.map(c => {
          const title = conversationTitle(c, newLabel)
          const preview = conversationPreview(c)
          const date = formatDate(c.cozyMetadata?.updatedAt, lang)
          return (
            <li key={c._id}>
              <button
                type="button"
                aria-current={c._id === conversationId ? 'page' : undefined}
                className="app-conv-item"
                title={title}
                onClick={() => navigate(`/assistant/${c._id}`)}
              >
                <span className="app-conv-title">{title}</span>
                {preview && <span className="app-conv-preview">{preview}</span>}
                {date && <span className="app-conv-date">{date}</span>}
              </button>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}

export default AppSidebar
