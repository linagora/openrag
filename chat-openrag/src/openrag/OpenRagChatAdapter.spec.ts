import type { ChatModelRunResult } from '@assistant-ui/react'
import { createOpenRagChatAdapter } from './OpenRagChatAdapter'

const sseResponse = (text: string): Response => {
  const enc = new TextEncoder()
  const body = new ReadableStream({
    start(c) {
      c.enqueue(enc.encode(text))
      c.close()
    }
  })
  return new Response(body, { status: 200 })
}

const toThreadMessages = (q: string) => [
  { role: 'user' as const, content: [{ type: 'text' as const, text: q }] }
]

it('streams running then complete with normalized sources', async () => {
  const sse =
    'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":null}],"extra":"{}"}\n\n' +
    'data: {"choices":[{"delta":{"content":" there"},"finish_reason":null}],"extra":"{}"}\n\n' +
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"extra":"{\\"sources\\":[{\\"source_type\\":\\"web\\",\\"url\\":\\"https://e.com\\",\\"title\\":\\"E\\"}]}"}\n\n' +
    'data: [DONE]\n\n'
  const fetchSpy = jest.spyOn(global, 'fetch').mockResolvedValue(sseResponse(sse))

  const adapter = createOpenRagChatAdapter({ model: 'openrag-all' })
  const results: ChatModelRunResult[] = []
  const gen = adapter.run({ messages: toThreadMessages('hello') } as never) as AsyncGenerator<ChatModelRunResult>
  for await (const r of gen)
    results.push(r)

  const final = results[results.length - 1]
  // eslint-disable-next-line @typescript-eslint/no-non-null-assertion
  expect((final.content![0] as { type: 'text'; text: string }).text).toBe('Hi there')
  expect(final.status).toEqual({ type: 'complete', reason: 'stop' })
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  expect((final.metadata as any).custom.sources[0]).toMatchObject({
    sourceType: 'web',
    url: 'https://e.com'
  })

  // request shape: full history, stream true, model carried
  const body = JSON.parse((fetchSpy.mock.calls[0][1] as RequestInit).body as string)
  expect(body).toMatchObject({ model: 'openrag-all', stream: true })
  expect(body.messages[0].content).toBe('hello')
  fetchSpy.mockRestore()
})

it('yields an error result when the request fails', async () => {
  jest.spyOn(global, 'fetch').mockRejectedValue(new Error('boom'))
  const adapter = createOpenRagChatAdapter({ model: 'openrag-all' })
  const results: ChatModelRunResult[] = []
  const gen = adapter.run({ messages: toThreadMessages('x') } as never) as AsyncGenerator<ChatModelRunResult>
  for await (const r of gen)
    results.push(r)
  expect(results[results.length - 1].status).toMatchObject({ type: 'incomplete' })
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  expect((results[results.length - 1].metadata as any).custom.isError).toBe(true)
  jest.restoreAllMocks()
})
