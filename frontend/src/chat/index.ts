/**
 * Public surface of the chat feature. The editor mounts `ChatDrawer`; the
 * router lazy-loads `ChatPage` and `NewChatPage` straight from their files.
 */
export { ChatDrawer, findOrCreateEditSession } from './ChatDrawer'
export type { ChatDrawerProps } from './ChatDrawer'
export { default as ChatPage } from './ChatPage'
export { default as NewChatPage } from './NewChatPage'
export { ChatView } from './ChatView'
export { useChatSession } from './useChatSession'
export type { ChatSessionApi, UseChatSessionOptions } from './useChatSession'
export { FormattedText, parseBlocks, parseInline } from './safeFormat'
export { chatReducer, initialChatState, selectThread } from './chatReducer'
export { CardRenderer, CARD_COMPONENTS } from './cards'
export * from './types'
