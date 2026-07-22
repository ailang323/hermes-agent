import { QueryClient } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { codexUsageQueryOptions } from './codex-usage-query'

const mocks = vi.hoisted(() => ({
  getCodexUsage: vi.fn()
}))

vi.mock('@/hermes', () => ({
  getCodexUsage: mocks.getCodexUsage
}))

describe('codexUsageQueryOptions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('binds each cache key to the same immutable request profile', async () => {
    mocks.getCodexUsage.mockResolvedValue({ available: false, provider: 'openai-codex', windows: [] })
    const profileA = codexUsageQueryOptions({ profile: 'profile-a', provider: 'openai-codex' })
    const profileB = codexUsageQueryOptions({ profile: 'profile-b', provider: 'openai-codex' })

    expect(profileA.queryKey).toEqual(['codex-usage', 'profile-a', 'openai-codex'])
    expect(profileB.queryKey).toEqual(['codex-usage', 'profile-b', 'openai-codex'])

    if (typeof profileA.queryFn !== 'function' || typeof profileB.queryFn !== 'function') {
      throw new TypeError('Codex usage query function is unavailable')
    }

    const requestA = profileA.queryFn({} as never)
    const requestB = profileB.queryFn({} as never)
    await Promise.all([requestB, requestA])

    expect(mocks.getCodexUsage).toHaveBeenNthCalledWith(1, 'profile-a')
    expect(mocks.getCodexUsage).toHaveBeenNthCalledWith(2, 'profile-b')
  })

  it('keeps in-flight profile responses in their matching caches', async () => {
    let resolveA!: (value: object) => void
    let resolveB!: (value: object) => void

    const responseA = new Promise<object>(resolve => {
      resolveA = resolve
    })

    const responseB = new Promise<object>(resolve => {
      resolveB = resolve
    })

    mocks.getCodexUsage.mockImplementation((profile: string) => (profile === 'profile-a' ? responseA : responseB))

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const optionsA = codexUsageQueryOptions({ profile: 'profile-a', provider: 'openai-codex' })
    const optionsB = codexUsageQueryOptions({ profile: 'profile-b', provider: 'openai-codex' })
    const requestA = client.fetchQuery(optionsA)
    const requestB = client.fetchQuery(optionsB)

    resolveB({ account_email: 'b@example.test', available: true, provider: 'openai-codex', windows: [] })
    resolveA({ account_email: 'a@example.test', available: true, provider: 'openai-codex', windows: [] })
    await Promise.all([requestB, requestA])

    expect(client.getQueryData(optionsA.queryKey)).toMatchObject({ account_email: 'a@example.test' })
    expect(client.getQueryData(optionsB.queryKey)).toMatchObject({ account_email: 'b@example.test' })
  })
})
