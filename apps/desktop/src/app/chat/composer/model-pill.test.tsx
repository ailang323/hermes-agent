import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'
import { setCurrentModel, setCurrentProvider } from '@/store/session'

import { ModelPill } from './model-pill'

vi.mock('./codex-quota-card', () => ({
  CodexQuotaCard: ({ profile }: { profile: string }) => <div data-testid="codex-quota-card">{profile}</div>
}))

describe('ModelPill Codex quota', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        disconnect() {}
        observe() {}
        unobserve() {}
      }
    )
    setCurrentModel('gpt-5.6-sol')
    setCurrentProvider('openai-codex')
    $activeGatewayProfile.set('luxuryai-dev')
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    setCurrentModel('')
    setCurrentProvider('')
    $activeGatewayProfile.set('default')
  })

  it('opens the profile-scoped quota card on hover', async () => {
    render(
      <ModelPill
        disabled={false}
        model={{
          canSwitch: true,
          model: 'gpt-5.6-sol',
          modelMenuContent: <div>Models</div>,
          provider: 'openai-codex'
        }}
      />
    )

    const button = screen.getByRole('button')
    fireEvent.mouseEnter(button.parentElement ?? button)

    expect((await screen.findByTestId('codex-quota-card')).textContent).toBe('luxuryai-dev')
  })
})
