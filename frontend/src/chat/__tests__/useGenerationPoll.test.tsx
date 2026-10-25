import { renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import {
  GENERATION_POLL_INTERVAL_MS,
  GENERATION_POLL_MAX_ATTEMPTS,
  GenerationFailedError,
  StillRunningError,
  pollUntilDone,
  useGenerationPoll,
} from '../../lib/useGenerationPoll'
import type { GeneratedProtocol } from '../../types'

const proto = (generation_status: GeneratedProtocol['generation_status'], extra: Partial<GeneratedProtocol> = {}) =>
  ({ id: 'p1', generation_status, ...extra }) as GeneratedProtocol

describe('pollUntilDone', () => {
  it('uses a 3 s interval and 600 attempts', () => {
    expect(GENERATION_POLL_INTERVAL_MS).toBe(3000)
    expect(GENERATION_POLL_MAX_ATTEMPTS).toBe(600)
  })

  it('checks at once, then waits between polls, and resolves with the finished protocol', async () => {
    const sleep = vi.fn(async () => {})
    const fetchProtocol = vi.fn()
      .mockResolvedValueOnce(proto('generating'))
      .mockResolvedValueOnce(proto('generating'))
      .mockResolvedValueOnce(proto('complete'))
    const done = await pollUntilDone('p1', { fetchProtocol, sleep })
    expect(done.generation_status).toBe('complete')
    expect(fetchProtocol).toHaveBeenCalledTimes(3)
    expect(sleep).toHaveBeenCalledTimes(2)
    expect(sleep).toHaveBeenCalledWith(3000, undefined)
  })

  it('treats a protocol with no status as complete (every older protocol)', async () => {
    const done = await pollUntilDone('p1', { fetchProtocol: async () => proto(undefined), sleep: async () => {} })
    expect(done.id).toBe('p1')
  })

  it('keeps polling through network blips', async () => {
    const fetchProtocol = vi.fn()
      .mockRejectedValueOnce(new Error('Failed to fetch'))
      .mockRejectedValueOnce(new Error('Failed to fetch'))
      .mockResolvedValueOnce(proto('complete'))
    await expect(pollUntilDone('p1', { fetchProtocol, sleep: async () => {} })).resolves.toBeDefined()
  })

  it('rejects with the server’s message when generation failed', async () => {
    await expect(
      pollUntilDone('p1', { fetchProtocol: async () => proto('failed', { generation_error: 'PubMed unreachable' }), sleep: async () => {} }),
    ).rejects.toThrow(new GenerationFailedError('PubMed unreachable'))
  })

  it('says "still running", not "failed", when it stops waiting', async () => {
    const fetchProtocol = vi.fn(async () => proto('generating'))
    await expect(pollUntilDone('p1', { fetchProtocol, sleep: async () => {}, maxAttempts: 5 })).rejects.toBeInstanceOf(StillRunningError)
    expect(fetchProtocol).toHaveBeenCalledTimes(5)
  })

  it('stops when aborted', async () => {
    const controller = new AbortController()
    controller.abort()
    await expect(pollUntilDone('p1', { fetchProtocol: async () => proto('generating'), signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
  })
})

describe('useGenerationPoll', () => {
  it('walks running -> complete', async () => {
    const fetchProtocol = vi.fn().mockResolvedValueOnce(proto('generating')).mockResolvedValueOnce(proto('complete'))
    const { result } = renderHook(() => useGenerationPoll('p1', { fetchProtocol, sleep: async () => {} }))
    await waitFor(() => expect(result.current.phase).toBe('complete'))
    expect(result.current.protocol?.id).toBe('p1')
  })

  it('reports a server failure with its text', async () => {
    const { result } = renderHook(() => useGenerationPoll('p1', { fetchProtocol: async () => proto('failed', { generation_error: 'Model refused' }), sleep: async () => {} }))
    await waitFor(() => expect(result.current.phase).toBe('failed'))
    expect(result.current.error).toBe('Model refused')
  })

  it('reports still_running after the attempt ceiling, never failed', async () => {
    const { result } = renderHook(() => useGenerationPoll('p1', { fetchProtocol: async () => proto('generating'), sleep: async () => {}, maxAttempts: 4 }))
    await waitFor(() => expect(result.current.phase).toBe('still_running'))
    expect(result.current.error).toBeNull()
  })

  it('does not poll while disabled and stops on unmount', async () => {
    const fetchProtocol = vi.fn(async () => proto('generating'))
    const idle = renderHook(() => useGenerationPoll('p1', { fetchProtocol, enabled: false }))
    expect(idle.result.current.phase).toBe('idle')
    expect(fetchProtocol).not.toHaveBeenCalled()

    let calls = 0
    const slow = renderHook(() =>
      useGenerationPoll('p2', {
        fetchProtocol: async () => {
          calls++
          return proto('generating')
        },
        sleep: (_ms, signal) => new Promise((_resolve, reject) => signal?.addEventListener('abort', () => reject(Object.assign(new Error('Aborted'), { name: 'AbortError' })))),
      }),
    )
    await waitFor(() => expect(calls).toBe(1))
    slow.unmount()
    await new Promise(r => setTimeout(r, 20))
    expect(calls).toBe(1)
  })
})
