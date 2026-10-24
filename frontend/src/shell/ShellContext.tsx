/**
 * State shared by the shell chrome (sidebar, top bar) and the pages under it:
 * layout (collapsed / drawer), the caller's capabilities, the org's protocol
 * list (polled lightly so the top bar can show generation activity), recent
 * chats, and a tiny toast.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { api } from '../api/client'
import type { Capabilities } from '../api/client'
import type { ProtocolSummary } from '../types'
import { useActingUser } from '../lib/actingUser'
import { useCapabilities } from '../lib/useCapabilities'
import { fetchRecentChats } from './chatList'
import type { RecentChats } from './chatList'
import { LG_QUERY, MD_QUERY, useMediaQuery } from './useMediaQuery'
import { useSidebarCollapsed } from './useSidebarCollapsed'

/** The list route may carry the async-generation status once the backend adds it. */
export type ProtocolRow = ProtocolSummary & { generation_status?: string }

export function isGenerating(p: ProtocolRow): boolean {
  return p.generation_status === 'generating'
}

/** How often the protocol list is refreshed while the tab is visible. */
export const PROTOCOL_POLL_MS = 15_000
/** How often recent chats are refreshed while the tab is visible. */
export const CHAT_POLL_MS = 60_000

export interface ShellApi {
  /** The user's saved preference (desktop sidebar as an icon rail). */
  collapsed: boolean
  toggleCollapsed: () => void
  isMd: boolean
  isLg: boolean
  /** The desktop sidebar is currently the 64px icon rail (preference, or too narrow for the full one). */
  rail: boolean
  drawerOpen: boolean
  /** Pass the control that was activated so focus can return to it on close
   *  (Safari does not focus a button on click, so `activeElement` is not enough). */
  openDrawer: (opener?: HTMLElement | null) => void
  closeDrawer: () => void
  /** The element focus returns to when the drawer closes. */
  drawerOpenerRef: { current: HTMLElement | null }

  capabilities: Capabilities | null
  protocols: ProtocolRow[] | null
  protocolsError: string | null
  refreshProtocols: () => Promise<void>
  /** Any protocol with generation_status === 'generating'. */
  generating: boolean
  recentChats: RecentChats | null

  copyInviteLink: () => Promise<void>
  toast: string | null
  notify: (message: string) => void
}

const ShellContext = createContext<ShellApi | null>(null)

export function useShell(): ShellApi {
  const ctx = useContext(ShellContext)
  if (!ctx) throw new Error('useShell must be used inside <ShellProvider>.')
  return ctx
}

/** Polls `fn` while the tab is visible: now, every `ms`, and again on becoming visible. */
function useVisiblePolling(fn: () => void, ms: number, enabled: boolean, resetKey: string | null) {
  const fnRef = useRef(fn)
  fnRef.current = fn
  useEffect(() => {
    if (!enabled) return
    let timer: ReturnType<typeof setInterval> | null = null
    const visible = () => typeof document === 'undefined' || document.visibilityState !== 'hidden'
    const stop = () => {
      if (timer) clearInterval(timer)
      timer = null
    }
    const start = () => {
      stop()
      timer = setInterval(() => {
        if (visible()) fnRef.current()
      }, ms)
    }
    const onVisibility = () => {
      if (visible()) {
        fnRef.current()
        start()
      } else {
        stop()
      }
    }
    if (visible()) fnRef.current()
    start()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [ms, enabled, resetKey])
}

export function ShellProvider({ children }: { children: ReactNode }) {
  const { pathname } = useLocation()
  const { currentUser } = useActingUser()
  const userId = currentUser?.id ?? null
  const { capabilities } = useCapabilities()

  const [collapsed, setCollapsed] = useSidebarCollapsed()
  const isMd = useMediaQuery(MD_QUERY)
  const isLg = useMediaQuery(LG_QUERY)
  const [drawerOpen, setDrawerOpen] = useState(false)

  // The drawer is a small-screen device: leaving that range, or navigating, closes it.
  useEffect(() => {
    setDrawerOpen(false)
  }, [pathname])
  useEffect(() => {
    if (isLg) setDrawerOpen(false)
  }, [isLg])

  // ── protocols (Home list + generation-activity dot) ──────────────────
  const [protocols, setProtocols] = useState<ProtocolRow[] | null>(null)
  const [protocolsError, setProtocolsError] = useState<string | null>(null)
  const protocolSeq = useRef(0)

  const refreshProtocols = useCallback(async () => {
    if (!userId) return
    const seq = ++protocolSeq.current
    try {
      const list = await api.listProtocols()
      if (seq !== protocolSeq.current) return
      setProtocols(list as ProtocolRow[])
      setProtocolsError(null)
    } catch (e) {
      if (seq !== protocolSeq.current) return
      setProtocolsError(e instanceof Error ? e.message : 'Could not load protocols.')
    }
  }, [userId])

  useEffect(() => {
    // New identity: do not keep showing the previous user's list.
    setProtocols(null)
    setProtocolsError(null)
    return () => {
      protocolSeq.current++
    }
  }, [userId])
  useVisiblePolling(() => void refreshProtocols(), PROTOCOL_POLL_MS, !!userId, userId)

  // ── recent chats ─────────────────────────────────────────────────────
  const [recentChats, setRecentChats] = useState<RecentChats | null>(null)
  const chatSeq = useRef(0)
  const refreshChats = useCallback(async () => {
    if (!userId) return
    const seq = ++chatSeq.current
    const result = await fetchRecentChats(5)
    if (seq === chatSeq.current) setRecentChats(result)
  }, [userId])
  useEffect(() => {
    setRecentChats(null)
    return () => {
      chatSeq.current++
    }
  }, [userId])
  useVisiblePolling(() => void refreshChats(), CHAT_POLL_MS, !!userId, userId)

  // ── toast ────────────────────────────────────────────────────────────
  const [toast, setToast] = useState<string | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const notify = useCallback((message: string) => {
    setToast(message)
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 3200)
  }, [])
  useEffect(
    () => () => {
      if (toastTimer.current) clearTimeout(toastTimer.current)
    },
    [],
  )

  const copyInviteLink = useCallback(async () => {
    try {
      const mine = await api.getMyOrg()
      if (!mine.join_token) {
        notify('Only the organization owner has an invite link.')
        return
      }
      const link = `${window.location.origin}/join/${mine.join_token}`
      await navigator.clipboard.writeText(link)
      notify('Invite link copied')
    } catch {
      notify('Could not copy the invite link. Open Team & seats to copy it.')
    }
  }, [notify])

  const generating = useMemo(() => (protocols ?? []).some(isGenerating), [protocols])

  const toggleCollapsed = useCallback(() => setCollapsed(prev => !prev), [setCollapsed])
  const drawerOpenerRef = useRef<HTMLElement | null>(null)
  const openDrawer = useCallback((opener?: HTMLElement | null) => {
    drawerOpenerRef.current = opener ?? (document.activeElement as HTMLElement | null)
    setDrawerOpen(true)
  }, [])
  const closeDrawer = useCallback(() => setDrawerOpen(false), [])

  const value = useMemo<ShellApi>(
    () => ({
      collapsed,
      toggleCollapsed,
      isMd,
      isLg,
      rail: collapsed || !isLg,
      drawerOpen,
      openDrawer,
      closeDrawer,
      drawerOpenerRef,
      capabilities,
      protocols,
      protocolsError,
      refreshProtocols,
      generating,
      recentChats,
      copyInviteLink,
      toast,
      notify,
    }),
    [
      collapsed, toggleCollapsed, isMd, isLg, drawerOpen, openDrawer, closeDrawer, drawerOpenerRef, capabilities,
      protocols, protocolsError, refreshProtocols, generating, recentChats, copyInviteLink, toast, notify,
    ],
  )

  return <ShellContext.Provider value={value}>{children}</ShellContext.Provider>
}
