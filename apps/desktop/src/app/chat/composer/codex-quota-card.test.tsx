import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CodexQuotaCard } from './codex-quota-card'

const mocks = vi.hoisted(() => ({
  getCodexUsage: vi.fn()
}))

vi.mock('@/hermes', () => ({
  getCodexUsage: mocks.getCodexUsage
}))

describe('CodexQuotaCard', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('shows profile-scoped remaining quota', async () => {
    mocks.getCodexUsage.mockResolvedValue({
      account_email: 'codex@example.test',
      available: true,
      details: [],
      error: null,
      plan: 'Plus',
      provider: 'openai-codex',
      windows: [{ label: 'Session', remaining_percent: 87, used_percent: 13 }]
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(
      <QueryClientProvider client={client}>
        <CodexQuotaCard enabled profile="luxuryai-dev" provider="openai-codex" />
      </QueryClientProvider>
    )

    expect(await screen.findByText('87% left')).toBeTruthy()
    expect(screen.getByText('codex@example.test')).toBeTruthy()
    expect(screen.getByText('luxuryai-dev')).toBeTruthy()
    expect(mocks.getCodexUsage).toHaveBeenCalledOnce()
    expect(mocks.getCodexUsage).toHaveBeenCalledWith('luxuryai-dev')
  })
})
