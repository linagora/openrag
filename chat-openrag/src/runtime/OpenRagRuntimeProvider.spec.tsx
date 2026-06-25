/// <reference types="@testing-library/jest-dom" />
import { render, screen } from '@testing-library/react'
import React from 'react'

let capturedInitial: unknown = null
jest.mock('@assistant-ui/react', () => ({
  AssistantRuntimeProvider: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  useLocalRuntime: (_adapter: unknown, opts: { initialMessages: unknown }) => {
    capturedInitial = opts.initialMessages
    return {}
  }
}))
jest.mock('../runtime/ModelContext', () => ({ useModel: () => ({ model: 'openrag-all' }) }))

import { OpenRagRuntimeProvider } from './OpenRagRuntimeProvider'

const fakeStore = {
  useConversationMessages: (_id: string) => ({
    messages: [{ id: 'm1', role: 'user', content: 'prior' }],
    isLoading: false
  }),
  appendMessages: jest.fn()
} as never

it('seeds the runtime with persisted messages', () => {
  render(
    <OpenRagRuntimeProvider conversationId="c1" store={fakeStore}>
      <div>thread</div>
    </OpenRagRuntimeProvider>
  )
  expect(screen.getByText('thread')).toBeInTheDocument()
  expect((capturedInitial as { content: unknown }[])[0]).toMatchObject({ role: 'user' })
})
