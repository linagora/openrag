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

const makeStore = (
  messages: { id?: string; role: string; content: string; sources?: unknown[] }[],
  isLoading = false
): never =>
  ({
    useConversationMessages: (_id: string) => ({ messages, isLoading }),
    appendMessages: jest.fn()
  }) as never

beforeEach(() => {
  capturedInitial = null
})

it('seeds the runtime with persisted messages', () => {
  const store = makeStore([{ id: 'm1', role: 'user', content: 'prior' }])
  render(
    <OpenRagRuntimeProvider conversationId="c1" store={store}>
      <div>thread</div>
    </OpenRagRuntimeProvider>
  )
  expect(screen.getByText('thread')).toBeInTheDocument()
  expect((capturedInitial as { content: unknown }[])[0]).toMatchObject({ role: 'user' })
})

it('renders nothing while isLoading is true (gate)', () => {
  const store = makeStore([{ id: 'm1', role: 'user', content: 'prior' }], true)
  render(
    <OpenRagRuntimeProvider conversationId="c1" store={store}>
      <div>thread</div>
    </OpenRagRuntimeProvider>
  )
  expect(capturedInitial).toBeNull()
  expect(screen.queryByText('thread')).not.toBeInTheDocument()
})

it('maps sources onto assistant message metadata', () => {
  const sources = [{ url: 'https://example.com', title: 'Example' }]
  const store = makeStore([
    { id: 'm1', role: 'assistant', content: 'answer', sources }
  ])
  render(
    <OpenRagRuntimeProvider conversationId="c1" store={store}>
      <div>thread</div>
    </OpenRagRuntimeProvider>
  )
  const msgs = capturedInitial as { metadata?: { custom?: { sources?: unknown } } }[]
  expect(msgs[0].metadata?.custom?.sources).toEqual(sources)
})

it('does not add metadata to user messages or assistant messages without sources', () => {
  const store = makeStore([
    { id: 'm1', role: 'user', content: 'question' },
    { id: 'm2', role: 'assistant', content: 'no-sources-answer' }
  ])
  render(
    <OpenRagRuntimeProvider conversationId="c1" store={store}>
      <div>thread</div>
    </OpenRagRuntimeProvider>
  )
  const msgs = capturedInitial as { metadata?: unknown }[]
  expect(msgs[0].metadata).toBeUndefined()
  expect(msgs[1].metadata).toBeUndefined()
})
