import { afterEach, describe, expect, it } from 'vitest'

import { setRuntimeI18nLocale } from '@/i18n/runtime'

import { host } from './index'

describe('plugin SDK host locale state', () => {
  afterEach(() => setRuntimeI18nLocale('en'))

  it('exposes the Hermes display locale reactively', () => {
    setRuntimeI18nLocale('zh')

    expect(host.state.locale.get()).toBe('zh')
  })
})
