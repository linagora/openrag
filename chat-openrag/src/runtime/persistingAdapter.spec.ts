import type { ChatModelAdapter, ChatModelRunResult } from '@assistant-ui/react'

import { createPersistingAdapter } from './persistingAdapter'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Build a minimal ChatModelRunResult */
function makeResult(
  text: string,
  statusType: 'complete' | 'incomplete' | 'error',
  sources?: unknown[]
): ChatModelRunResult {
  return {
    content: [{ type: 'text', text }],
    status: { type: statusType, ...(statusType !== 'complete' ? { reason: 'cancelled' } : {}) } as ChatModelRunResult['status'],
    ...(sources !== undefined
      ? { metadata: { custom: { sources } } }
      : {})
  }
}

/** Create an async-generator base adapter that yields the given results */
function makeBaseAdapter(results: ChatModelRunResult[]): ChatModelAdapter {
  return {
    async *run() {
      for (const r of results) {
        yield r
      }
    }
  }
}

/** Build a minimal ChatModelRunOptions-like object */
function makeOpts(userText: string) {
  return {
    messages: [
      { role: 'user', content: [{ type: 'text', text: userText }] }
    ],
    runConfig: {},
    abortSignal: new AbortController().signal,
    context: {} as never,
    config: {} as never,
    unstable_getMessage: () => ({ role: 'user' } as never)
  } as never
}

/** Drain an async-generator and return all yielded values */
async function drain(adapter: ChatModelAdapter, opts: ReturnType<typeof makeOpts>) {
  const results: ChatModelRunResult[] = []
  const gen = adapter.run(opts)
  if (Symbol.asyncIterator in gen) {
    for await (const r of gen as AsyncGenerator<ChatModelRunResult>) {
      results.push(r)
    }
  } else {
    results.push(await gen)
  }
  return results
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('createPersistingAdapter', () => {
  const makeStore = () => ({
    appendMessages: jest.fn().mockResolvedValue(undefined),
    useConversationMessages: jest.fn(),
    getConversations: jest.fn(),
    createConversation: jest.fn(),
    deleteConversation: jest.fn()
  })

  it('complete WITH sources: calls appendMessages once with user + assistant messages including sources', async () => {
    const sources = [{ url: 'https://example.com', title: 'Ex' }]
    const runningResult = makeResult('partial answer', 'incomplete')
    const completeResult = makeResult('final answer', 'complete', sources)
    const base = makeBaseAdapter([runningResult, completeResult])
    const store = makeStore()

    const adapter = createPersistingAdapter(base as never, store as never, 'conv-1')
    const yielded = await drain(adapter as never, makeOpts('hello'))

    // streaming is unaffected — all base results are forwarded
    expect(yielded).toHaveLength(2)
    expect(yielded[0]).toBe(runningResult)
    expect(yielded[1]).toBe(completeResult)

    // persistence: called exactly once
    expect(store.appendMessages).toHaveBeenCalledTimes(1)
    const [convId, msgs] = store.appendMessages.mock.calls[0]
    expect(convId).toBe('conv-1')
    expect(msgs).toHaveLength(2)
    expect(msgs[0]).toMatchObject({ role: 'user', content: 'hello' })
    expect(msgs[1]).toMatchObject({ role: 'assistant', content: 'final answer', sources })
  })

  it('complete WITHOUT sources: calls appendMessages with 2 msgs, no sources key', async () => {
    const completeResult = makeResult('answer without sources', 'complete')
    const base = makeBaseAdapter([completeResult])
    const store = makeStore()

    const adapter = createPersistingAdapter(base as never, store as never, 'conv-2')
    const yielded = await drain(adapter as never, makeOpts('question'))

    // streaming unaffected
    expect(yielded).toHaveLength(1)
    expect(yielded[0]).toBe(completeResult)

    expect(store.appendMessages).toHaveBeenCalledTimes(1)
    const [, msgs] = store.appendMessages.mock.calls[0]
    expect(msgs).toHaveLength(2)
    expect(msgs[0]).toMatchObject({ role: 'user', content: 'question' })
    expect(msgs[1]).toMatchObject({ role: 'assistant', content: 'answer without sources' })
    expect('sources' in msgs[1]).toBe(false)
  })

  it('non-complete (error/incomplete): does NOT call appendMessages', async () => {
    const incompleteResult = makeResult('partial', 'incomplete')
    const base = makeBaseAdapter([incompleteResult])
    const store = makeStore()

    const adapter = createPersistingAdapter(base as never, store as never, 'conv-3')
    const yielded = await drain(adapter as never, makeOpts('anything'))

    // streaming still unaffected
    expect(yielded).toHaveLength(1)
    expect(yielded[0]).toBe(incompleteResult)

    // no persistence on non-complete
    expect(store.appendMessages).not.toHaveBeenCalled()
  })

  it('asserts streaming is unaffected: every yielded result equals what baseAdapter yielded', async () => {
    const r1 = makeResult('chunk 1', 'incomplete')
    const r2 = makeResult('chunk 2', 'incomplete')
    const r3 = makeResult('chunk 3', 'complete')
    const base = makeBaseAdapter([r1, r2, r3])
    const store = makeStore()

    const adapter = createPersistingAdapter(base as never, store as never, 'conv-4')
    const yielded = await drain(adapter as never, makeOpts('stream me'))

    expect(yielded).toStrictEqual([r1, r2, r3])
  })
})
