import { beforeEach, describe, expect, it, vi } from 'vitest'

const profileEnabledKey = (profile: string) => `hermes.desktop.composerPopout.v2.${encodeURIComponent(profile)}.enabled`
const profilePositionKey = (profile: string) => `hermes.desktop.composerPopout.v2.${encodeURIComponent(profile)}.position`

async function loadStores() {
  const profile = await import('./profile')
  const popout = await import('./composer-popout')

  return { popout, profile }
}

describe('composer popout profile-scoped persistence', () => {
  beforeEach(() => {
    vi.resetModules()
    window.localStorage.clear()
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 })
  })

  it('persists enabled state and position under the active profile only', async () => {
    const { popout, profile } = await loadStores()

    profile.$activeGatewayProfile.set('acewill-dev')
    popout.setComposerPoppedOut(true)
    popout.setComposerPopoutPosition({ bottom: 64, right: 72 }, { persist: true })

    expect(window.localStorage.getItem(profileEnabledKey('acewill-dev'))).toBe('true')
    expect(window.localStorage.getItem(profilePositionKey('acewill-dev'))).toBe(JSON.stringify({ bottom: 64, right: 72 }))

    profile.$activeGatewayProfile.set('aivideo-dev')
    expect(popout.$composerPoppedOut.get()).toBe(false)
    expect(popout.$composerPopoutPosition.get()).toEqual({ bottom: 24, right: 24 })

    profile.$activeGatewayProfile.set('acewill-dev')
    expect(popout.$composerPoppedOut.get()).toBe(true)
    expect(popout.$composerPopoutPosition.get()).toEqual({ bottom: 64, right: 72 })
  })
})
