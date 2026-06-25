import { parseSSE } from './sseStream'

const streamFrom = (text: string): ReadableStream<Uint8Array> => {
  const enc = new TextEncoder()
  // split mid-chunk to prove buffering across reads works
  const parts = [text.slice(0, 10), text.slice(10)]
  let i = 0
  return new ReadableStream({
    pull(controller) {
      if (i < parts.length) controller.enqueue(enc.encode(parts[i++]))
      else controller.close()
    }
  })
}

it('yields content chunks then stops on [DONE]', async () => {
  const sse =
    'data: {"choices":[{"delta":{"content":"Hel"},"finish_reason":null}],"extra":"{}"}\n\n' +
    'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":null}],"extra":"{}"}\n\n' +
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"extra":"{\\"sources\\":[]}"}\n\n' +
    'data: [DONE]\n\n'
  const out = []
  for await (const c of parseSSE(streamFrom(sse))) out.push(c)
  expect(out.map(c => c.choices[0].delta.content)).toEqual(['Hel', 'lo', undefined])
  expect(out[2].choices[0].finish_reason).toBe('stop')
  expect(out[2].extra).toBe('{"sources":[]}')
})
