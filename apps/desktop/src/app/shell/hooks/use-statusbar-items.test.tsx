import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'
import { setCurrentModel, setCurrentProvider } from '@/store/session'

import { useStatusbarItems } from './use-statusbar-items'

const mocks = vi.hoisted(() => ({
  getCodexUsage: vi.fn()
}))

vi.mock('@/hermes', () => ({
  getCodexUsage: mocks.getCodexUsage,
  setApiRequestProfile: vi.fn()
}))

describe('useStatusbarItems Codex quota', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false }
      }
    })
    setCurrentModel('gpt-5.6-sol')
    setCurrentProvider('openai-codex')
    $activeGatewayProfile.set('luxuryai-dev')
  })

  afterEach(() => {
    cleanup()
    queryClient.clear()
    setCurrentModel('')
    setCurrentProvider('')
    $activeGatewayProfile.set('default')
  })

  it('shows live remaining quota before context usage', async () => {
    mocks.getCodexUsage.mockResolvedValue({
      available: true,
      details: [],
      error: null,
      plan: 'Plus',
      provider: 'openai-codex',
      source: 'usage_api',
      windows: [
        {
          label: 'Session',
          remaining_percent: 87,
          used_percent: 13
        }
      ]
    })

    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    )

    const { result } = renderHook(
      () =>
        useStatusbarItems({
          agentsOpen: false,
          chatOpen: true,
          commandCenterOpen: false,
          extraLeftItems: [],
          extraRightItems: [],
          freshDraftReady: true,
          gatewayState: 'open',
          inferenceStatus: { checksDisagree: false, ready: true, reason: null, source: 'runtime_check' },
          openAgents: vi.fn(),
          openCommandCenterSection: vi.fn(),
          requestGateway: vi.fn(),
          statusSnapshot: null,
          toggleCommandCenter: vi.fn()
        }),
      { wrapper }
    )

    await waitFor(() => {
      expect(result.current.statusbarItems.find(item => item.id === 'codex-quota')?.label).toBe('87% left')
    })

    const quotaIndex = result.current.statusbarItems.findIndex(item => item.id === 'codex-quota')
    const contextIndex = result.current.statusbarItems.findIndex(item => item.id === 'context-usage')

    expect(quotaIndex).toBeGreaterThanOrEqual(0)
    expect(contextIndex).toBeGreaterThan(quotaIndex)
    expect(mocks.getCodexUsage).toHaveBeenCalledOnce()
    expect(mocks.getCodexUsage).toHaveBeenCalledWith('luxuryai-dev')
  })
})
