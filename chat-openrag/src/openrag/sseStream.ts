export interface OpenRagChunk {
  choices: { delta: { content?: string }; finish_reason: string | null }[]
  extra?: string
}

export async function* parseSSE(
  stream: ReadableStream<Uint8Array>
): AsyncGenerator<OpenRagChunk> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let nl: number
      while ((nl = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, nl).trim()
        buffer = buffer.slice(nl + 1)
        if (!line.startsWith('data:')) continue
        const payload = line.slice(5).trim()
        if (payload === '[DONE]') return
        try {
          yield JSON.parse(payload) as OpenRagChunk
        } catch {
          // ignore keep-alive / partial frames
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}
