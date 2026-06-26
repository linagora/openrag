import React from 'react'
import {
  BrowserRouter,
  Routes,
  Route,
  useNavigate,
  useParams
} from 'react-router-dom'

import { useI18n } from 'twake-i18n'

import {
  ConversationStoreProvider,
  ChatComponentsProvider,
  Conversation,
  Sidebar as SidebarRaw,
  AssistantProvider as AssistantProviderRaw
} from 'cozy-search/ai-chat-ui'

import AppProviders from './providers/AppProviders'
import OpenRagModelSelector from './components/OpenRagModelSelector'
import OpenRagSources from './components/OpenRagSources'
import { ModelProvider } from './runtime/ModelContext'
import { OpenRagRuntimeProvider } from './runtime/OpenRagRuntimeProvider'
import {
  useLocalConversationStore,
  type LocalStore
} from './store/LocalConversationStore'

// cozy-search ai-chat-ui ships loose types: Sidebar marks className required and
// AssistantProvider's .d.ts has no default-export type. Cast to the real shapes.
const Sidebar = SidebarRaw as unknown as React.ComponentType<{
  className?: string
}>
const AssistantProvider = AssistantProviderRaw as unknown as React.ComponentType<{
  children?: React.ReactNode
}>

const EmptyState = (): JSX.Element => {
  const navigate = useNavigate()
  const { t } = useI18n()
  const store = useLocalConversationStore()
  const onNew = async (): Promise<void> => {
    const id = await store.createConversation()
    navigate(`/assistant/${id}`)
  }
  return (
    <div className="app-empty">
      <h2 className="app-empty-title">{t('assistant.message.welcome')}</h2>
      <button type="button" className="app-new-btn" onClick={onNew}>
        + {t('assistant.sidebar.create_new')}
      </button>
    </div>
  )
}

const ChatRoute = ({ store }: { store: LocalStore }): JSX.Element => {
  const { conversationId } = useParams<{ conversationId: string }>()
  if (!conversationId) return <EmptyState />
  return (
    <OpenRagRuntimeProvider conversationId={conversationId} store={store}>
      <Conversation className="u-flex-auto" />
    </OpenRagRuntimeProvider>
  )
}

const Shell = (): JSX.Element => {
  const store = useLocalConversationStore()
  return (
    <ConversationStoreProvider store={store}>
      <ChatComponentsProvider
        components={{
          SourcesRenderer: OpenRagSources,
          ComposerExtras: OpenRagModelSelector
        }}
      >
        {/* AssistantProvider supplies the state that cozy-search's Sidebar and
            its useConversation hook read via useAssistant(). */}
        <AssistantProvider>
          <div className="app-shell">
            <Sidebar />
            <div className="app-main">
              <Routes>
                <Route
                  path="/assistant/:conversationId"
                  element={<ChatRoute store={store} />}
                />
                <Route path="*" element={<EmptyState />} />
              </Routes>
            </div>
          </div>
        </AssistantProvider>
      </ChatComponentsProvider>
    </ConversationStoreProvider>
  )
}

const App = (): JSX.Element => (
  <AppProviders>
    <ModelProvider>
      <BrowserRouter>
        <Shell />
      </BrowserRouter>
    </ModelProvider>
  </AppProviders>
)

export default App
