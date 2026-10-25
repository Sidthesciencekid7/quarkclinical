import { useShell } from '../shell/ShellContext'

const noop = (): void => {}

/** The shell's toast when the chat is mounted inside the app shell, a no-op anywhere else (tests, a standalone render). */
export function useNotify(): (message: string) => void {
  try {
    return useShell().notify
  } catch {
    return noop
  }
}
