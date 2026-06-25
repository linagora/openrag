import { render, screen, waitFor } from '@testing-library/react'
import React from 'react'

import { useModels } from './useModels'

// Wrapper component used to test the hook (renderHook is not in @testing-library/react v12).
const Probe = () => {
  const { models, isLoading } = useModels()
  return <div>{isLoading ? 'loading' : models.join(',')}</div>
}

afterEach(() => jest.restoreAllMocks())

it('loads model ids from /v1/models', async () => {
  jest.spyOn(global, 'fetch').mockResolvedValue(
    new Response(
      JSON.stringify({
        object: 'list',
        data: [
          { id: 'openrag-docs', object: 'model' },
          { id: 'openrag-all', object: 'model' }
        ]
      })
    )
  )
  render(React.createElement(Probe))
  // findByText throws if not found — sufficient to assert presence
  await screen.findByText('openrag-docs,openrag-all')
})

it('sets isLoading to false even when fetch fails', async () => {
  jest.spyOn(global, 'fetch').mockRejectedValue(new Error('network error'))
  render(React.createElement(Probe))
  await waitFor(() =>
    expect(screen.queryByText('loading')).toBeNull()
  )
})
