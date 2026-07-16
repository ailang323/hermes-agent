import { beforeEach, describe, expect, it } from 'vitest'

import {
  $composerContextReferences,
  composerContextBlocksFromDraft,
  reconcileComposerContextReferences,
  setComposerSelectionReference
} from '@/store/composer'

describe('selection context references', () => {
  beforeEach(() => $composerContextReferences.set({}))

  it('expands a selection chip into hidden prompt context', () => {
    setComposerSelectionReference('_selection', 'Selected assistant text')
    expect(composerContextBlocksFromDraft('@selection:`_selection` use this')).toEqual([
      'Selected context (_selection):\n```text\nSelected assistant text\n```'
    ])
  })

  it('drops hidden context after the chip leaves the draft', () => {
    setComposerSelectionReference('_selection', 'Selected assistant text')
    reconcileComposerContextReferences('plain draft')
    expect($composerContextReferences.get()).toEqual({})
  })
})
