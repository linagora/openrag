import { fireEvent, render, screen } from '@testing-library/react'
import React from 'react'

import OpenRagModelSelector from './OpenRagModelSelector'
import { ModelProvider } from '../runtime/ModelContext'

afterEach(() => jest.restoreAllMocks())

it('defaults to the first model and lets the user switch', async () => {
  jest.spyOn(global, 'fetch').mockResolvedValue(
    new Response(
      JSON.stringify({ data: [{ id: 'openrag-docs' }, { id: 'openrag-all' }] })
    )
  )
  render(
    <ModelProvider>
      <OpenRagModelSelector />
    </ModelProvider>
  )
  const select = (await screen.findByLabelText('Partition')) as HTMLSelectElement
  expect(select.value).toBe('openrag-docs')
  fireEvent.change(select, { target: { value: 'openrag-all' } })
  expect(select.value).toBe('openrag-all')
})

it('renders null when no models are available', async () => {
  jest.spyOn(global, 'fetch').mockResolvedValue(
    new Response(JSON.stringify({ data: [] }))
  )
  const { container } = render(
    <ModelProvider>
      <OpenRagModelSelector />
    </ModelProvider>
  )
  // Wait for fetch to resolve then assert nothing rendered
  await new Promise(r => setTimeout(r, 50))
  expect(container.querySelector('select')).toBeNull()
})

it('labels openrag-all as "All partitions" and strips prefix otherwise', async () => {
  jest.spyOn(global, 'fetch').mockResolvedValue(
    new Response(
      JSON.stringify({
        data: [{ id: 'openrag-docs' }, { id: 'openrag-all' }]
      })
    )
  )
  render(
    <ModelProvider>
      <OpenRagModelSelector />
    </ModelProvider>
  )
  await screen.findByLabelText('Partition')
  // getByRole throws if not found — sufficient to assert presence
  expect(screen.getByRole('option', { name: 'docs' }).textContent).toBe('docs')
  expect(screen.getByRole('option', { name: 'All partitions' }).textContent).toBe('All partitions')
})
