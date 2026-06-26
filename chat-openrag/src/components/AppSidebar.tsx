import React from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import ButtonRaw from 'cozy-ui/transpiled/react/Buttons'
import IconRaw from 'cozy-ui/transpiled/react/Icon'
import PlusIcon from 'cozy-ui/transpiled/react/Icons/Plus'
import TypographyRaw from 'cozy-ui/transpiled/react/Typography'
import { useI18n } from 'twake-i18n'
import { ConversationList, useConversationStore } from 'cozy-search/decoupled'

// cozy-ui transpiled components have inconsistent types (some propless
// forwardRefs); cozy-search consumes them from untyped .jsx. Cast permissively.
/* eslint-disable @typescript-eslint/no-explicit-any */
const Button = ButtonRaw as unknown as React.ComponentType<any>
const Icon = IconRaw as unknown as React.ComponentType<any>
const Typography = TypographyRaw as unknown as React.ComponentType<any>
/* eslint-enable @typescript-eslint/no-explicit-any */

const AppSidebar = (): JSX.Element => {
  const navigate = useNavigate()
  const { t } = useI18n()
  const { conversationId } = useParams<{ conversationId: string }>()
  const store = useConversationStore()
  const { conversations } = store.useConversations()

  const onNew = async (): Promise<void> => {
    const id = await store.createConversation()
    navigate(`/assistant/${id}`)
  }

  return (
    <nav className="app-sidebar" aria-label="Conversations">
      <div className="u-ph-1 u-pv-1">
        <Button
          className="u-w-100 u-bdrs-6"
          label={t('assistant.sidebar.create_new')}
          startIcon={<Icon icon={PlusIcon} />}
          fullWidth
          variant="primary"
          onClick={onNew}
        />
      </div>
      <Typography variant="h6" className="u-ph-1 u-pv-half">
        {t('assistant.sidebar.recent_chats')}
      </Typography>
      <div className="app-sidebar-list">
        {/* Reuses cozy-search's ConversationList (cozy-ui List/ListItem,
            dividers, typography, helpers) with disableAction so the
            cozy-client-coupled rename/delete/share menu is hidden in v1. */}
        <ConversationList
          conversations={conversations}
          currentConversationId={conversationId}
          onOpenConversation={(id: string) => navigate(`/assistant/${id}`)}
          disableAction
          divider
        />
      </div>
    </nav>
  )
}

export default AppSidebar
