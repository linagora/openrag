import React from 'react'
import {
  BrowserRouter,
  Routes,
  Route,
  useNavigate,
  useParams
} from 'react-router-dom'

import {
  ConversationStoreProvider,
  ChatComponentsProvider,
  Conversation
} from 'cozy-search/decoupled'

import AppProviders from './providers/AppProviders'
import AppSidebar from './components/AppSidebar'
import OpenRagModelSelector from './components/OpenRagModelSelector'
import OpenRagSources from './components/OpenRagSources'
import { ModelProvider } from './runtime/ModelContext'
import { OpenRagRuntimeProvider } from './runtime/OpenRagRuntimeProvider'
import {
  useLocalConversationStore,
  type LocalStore
} from './store/LocalConversationStore'

const EmptyState = (): JSX.Element => {
  const navigate = useNavigate()
  const store = useLocalConversationStore()
  const onNew = async (): Promise<void> => {
    const id = await store.createConversation()
    navigate(`/assistant/${id}`)
  }
  return (
    <div className="u-flex u-flex-column u-flex-items-center u-flex-justify-center u-flex-auto">
      <button type="button" onClick={onNew}>
        New conversation
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
        <div className="u-flex u-h-100">
          <AppSidebar />
          <div className="u-flex-auto u-flex u-flex-column">
            <Routes>
              <Route
                path="/assistant/:conversationId"
                element={<ChatRoute store={store} />}
              />
              <Route path="*" element={<EmptyState />} />
            </Routes>
          </div>
        </div>
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
