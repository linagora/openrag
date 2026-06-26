import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'

import OpenRagModelSelector from './OpenRagModelSelector'
import { ModelProvider } from '../runtime/ModelContext'

afterEach(() => jest.restoreAllMocks())

const mockModels = (ids: string[]): void => {
  jest
    .spyOn(global, 'fetch')
    .mockResolvedValue(
      new Response(JSON.stringify({ data: ids.map(id => ({ id })) }))
    )
}

const renderSelector = (): ReturnType<typeof render> =>
  render(
    <ModelProvider>
      <OpenRagModelSelector />
    </ModelProvider>
  )

it('defaults to the first model and lets the user switch', async () => {
  mockModels(['openrag-docs', 'openrag-all'])
  renderSelector()
  // The chip shows the first model's label by default.
  const chip = await screen.findByRole('button', { name: /docs/ })
  expect(chip).toBeInTheDocument()
  // Open the dropdown and pick another partition.
  fireEvent.click(chip)
  fireEvent.click(screen.getByRole('option', { name: 'All partitions' }))
  // The chip label reflects the new selection (and the menu closed).
  expect(
    await screen.findByRole('button', { name: /All partitions/ })
  ).toBeInTheDocument()
})

it('renders nothing when no models are available', async () => {
  mockModels([])
  const { container } = renderSelector()
  await waitFor(() => {
    expect(container.querySelector('.openrag-selector')).toBeNull()
  })
})

it('labels openrag-all as "All partitions" and strips the prefix otherwise', async () => {
  mockModels(['openrag-docs', 'openrag-all'])
  renderSelector()
  const chip = await screen.findByRole('button', { name: /docs/ })
  fireEvent.click(chip)
  expect(screen.getByRole('option', { name: 'docs' })).toBeInTheDocument()
  expect(
    screen.getByRole('option', { name: 'All partitions' })
  ).toBeInTheDocument()
})
