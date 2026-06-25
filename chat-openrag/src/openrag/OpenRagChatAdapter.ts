import type {
  ChatModelAdapter,
  ChatModelRunOptions,
  ChatModelRunResult
} from '@assistant-ui/react'

import { apiFetch } from '../config'
import { normalizeSources } from './normalizeSources'
import { parseSSE } from './sseStream'

export interface OpenRagChatAdapterOptions {
  model: string
}

// assistant-ui ThreadMessage content is an array of parts; openRAG wants
// OpenAI-style { role, content: string }. Flatten text parts.
const toOpenAIMessages = (
  messages: ChatModelRunOptions['messages']
): { role: string; content: string }[] =>
  messages.map(m => ({
    role: m.role,
    content: m.content
      .filter((p): p is { type: 'text'; text: string } => p.type === 'text')
      .map(p => p.text)
      .join('')
  }))

export const createOpenRagChatAdapter = (
  options: OpenRagChatAdapterOptions
): ChatModelAdapter => ({
  async *run({
    messages,
    abortSignal
  }: ChatModelRunOptions): AsyncGenerator<ChatModelRunResult> {
    try {
      yield {
        content: [{ type: 'text', text: '' }],
        status: { type: 'requires-action', reason: 'tool-calls' }
      }

      const response = await apiFetch('/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: abortSignal,
        body: JSON.stringify({
          model: options.model,
          messages: toOpenAIMessages(messages),
          stream: true
        })
      })

      if (!response.ok || !response.body) {
        throw new Error(`openRAG ${response.status}`)
      }

      let fullText = ''
      let sources: ReturnType<typeof normalizeSources> = []

      for await (const chunk of parseSSE(response.body)) {
        if (abortSignal?.aborted) return
        const delta = chunk.choices?.[0]?.delta?.content
        if (delta) {
          fullText += delta
          yield {
            content: [{ type: 'text', text: fullText }],
            status: { type: 'running' }
          }
        }
        if (chunk.extra && chunk.extra !== '{}') {
          sources = normalizeSources(chunk.extra)
        }
      }

      yield {
        content: [{ type: 'text', text: fullText }],
        status: { type: 'complete', reason: 'stop' },
        ...(sources.length ? { metadata: { custom: { sources } } } : {})
      }
    } catch (error) {
      if (abortSignal?.aborted) return
      yield {
        content: [{ type: 'text', text: 'An error occurred.' }],
        status: { type: 'incomplete', reason: 'error' },
        metadata: { custom: { isError: true } }
      }
    }
  }
})
