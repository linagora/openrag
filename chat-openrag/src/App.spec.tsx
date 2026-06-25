/// <reference types="@testing-library/jest-dom" />
import { render, screen } from '@testing-library/react'
import React from 'react'

import App from './App'

// The default route ("/") matches the "*" route -> EmptyState + AppSidebar.
// This proves the full provider/router/store/chat-components composition boots
// without mounting the chat route's <Conversation /> (assistant-ui thread),
// which is exercised separately by the runtime provider spec. The "New
// conversation" affordance comes from both EmptyState and AppSidebar.
beforeEach(() => {
  jest
    .spyOn(global, 'fetch')
    .mockResolvedValue(new Response(JSON.stringify({ data: [{ id: 'openrag-all' }] })))
})

afterEach(() => {
  jest.restoreAllMocks()
})

it('boots and shows the new-conversation entry', async () => {
  render(<App />)
  expect((await screen.findAllByText(/New conversation/)).length).toBeGreaterThan(0)
})
