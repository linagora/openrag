// Stub for @assistant-ui/react — jest only; the real ESM build is used by rspack.
const React = require('react')

const noop = () => undefined
const identity = x => x
const useNoop = () => undefined

const Primitive = name => {
  const comp = ({ children }) => React.createElement(React.Fragment, null, children)
  comp.displayName = name
  return comp
}

const ThreadPrimitive = {
  Root: Primitive('ThreadPrimitive.Root'),
  Empty: Primitive('ThreadPrimitive.Empty'),
  Viewport: Primitive('ThreadPrimitive.Viewport'),
  Messages: Primitive('ThreadPrimitive.Messages'),
  ScrollToBottom: Primitive('ThreadPrimitive.ScrollToBottom'),
  If: Primitive('ThreadPrimitive.If')
}

const MessagePrimitive = {
  Root: Primitive('MessagePrimitive.Root'),
  Parts: Primitive('MessagePrimitive.Parts'),
  Content: Primitive('MessagePrimitive.Content'),
  If: Primitive('MessagePrimitive.If')
}

const ComposerPrimitive = {
  Root: Primitive('ComposerPrimitive.Root'),
  Input: Primitive('ComposerPrimitive.Input'),
  Send: Primitive('ComposerPrimitive.Send'),
  Cancel: Primitive('ComposerPrimitive.Cancel'),
  If: Primitive('ComposerPrimitive.If')
}

const ContentPartPrimitive = {
  Text: Primitive('ContentPartPrimitive.Text')
}

module.exports = {
  ThreadPrimitive,
  MessagePrimitive,
  ComposerPrimitive,
  ContentPartPrimitive,
  useThread: () => undefined,
  useMessage: () => undefined,
  useComposer: () => undefined,
  useComposerRuntime: () => ({ setText: noop, submit: noop }),
  useContentPart: () => undefined,
  makeAssistantToolUI: noop,
  AssistantRuntimeProvider: Primitive('AssistantRuntimeProvider'),
  Thread: Primitive('Thread')
}
