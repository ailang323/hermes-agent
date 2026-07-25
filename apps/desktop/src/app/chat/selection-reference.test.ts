import { beforeEach, describe, expect, it, vi } from 'vitest'

import { refChipElement, refChipHtml } from '@/app/chat/composer/rich-editor'
import { $queuedPromptsBySession, enqueueQueuedPrompt } from '@/store/composer-queue'
import {
  $composerContextReferences,
  clearComposerContextReferences,
  clearSessionDraft,
  composerContextBlocksFromDraft,
  composerContextReferenceText,
  CONTEXT_REFS_STORAGE_KEY,
  nextComposerContextReferenceLabel,
  reconcileComposerContextReferences,
  SESSION_DRAFTS_STORAGE_KEY,
  setComposerDraft,
  setComposerSelectionReference,
  stashSessionDraft
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

describe('selection chip hover preview', () => {
  beforeEach(() => $composerContextReferences.set({}))

  // The chip's visible label is the synthetic placeholder `_selection`, so the
  // quoted text has to ride along for the hover panel to render it. Deliberately
  // NOT `title`: a native tooltip appears after 1–2s and is often suppressed
  // entirely, which reads as "hovering does nothing".
  it('carries the quoted text as data-ref-preview, not title', () => {
    setComposerSelectionReference('_selection', 'Selected assistant text')

    const chip = refChipElement('selection', '`_selection`')

    expect(chip.dataset.refPreview).toBe('Selected assistant text')
    expect(chip.title).toBe('')
    expect(refChipHtml('selection', '`_selection`')).toContain('data-ref-preview="Selected assistant text"')
  })

  it('escapes markup in the quoted text', () => {
    setComposerSelectionReference('_selection', 'a <b> & "c"')
    expect(refChipHtml('selection', '`_selection`')).toContain('data-ref-preview="a &lt;b&gt; &amp; &quot;c&quot;"')
    expect(refChipElement('selection', '`_selection`').dataset.refPreview).toBe('a <b> & "c"')
  })

  it('truncates an over-long selection so the panel cannot cover the window', () => {
    setComposerSelectionReference('_selection', 'x'.repeat(2000))

    const preview = refChipElement('selection', '`_selection`').dataset.refPreview as string

    expect(preview).toHaveLength(601)
    expect(preview.endsWith('…')).toBe(true)
  })

  it('leaves ordinary refs without a preview — their label already says everything', () => {
    expect(refChipElement('file', '`src/app/main.ts`').dataset.refPreview).toBeUndefined()
    expect(refChipHtml('file', '`src/app/main.ts`')).not.toContain('data-ref-preview')
  })

  it('has no preview once the reference is gone from the store', () => {
    expect(composerContextReferenceText('selection', '_selection')).toBe('')
    expect(refChipElement('selection', '`_selection`').dataset.refPreview).toBeUndefined()
  })
})

describe('selection labels do not climb forever', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $queuedPromptsBySession.set({})
    $composerContextReferences.set({})
    clearSessionDraft(null)
    clearSessionDraft('other-session')
    setComposerDraft('')
  })

  it('numbers a second reference only while the first chip is still in a draft', () => {
    setComposerSelectionReference('_selection', 'first')
    setComposerDraft('@selection:`_selection` and')
    expect(nextComposerContextReferenceLabel('selection', '_selection')).toBe('_selection-2')
  })

  // The reported bug: the counter climbed on every quote because a deleted chip's
  // reference stayed in the map and its label was treated as taken forever.
  it('reuses the base label after the chip is deleted from the draft', () => {
    setComposerSelectionReference('_selection', 'first')
    setComposerDraft('@selection:`_selection` and')
    expect(nextComposerContextReferenceLabel('selection', '_selection')).toBe('_selection-2')

    setComposerDraft('')
    expect(nextComposerContextReferenceLabel('selection', '_selection')).toBe('_selection')
  })

  it('keeps a label taken while another session still shows that chip', () => {
    setComposerSelectionReference('_selection', 'first')
    stashSessionDraft('other-session', '@selection:`_selection` kept', [])
    setComposerDraft('')
    expect(nextComposerContextReferenceLabel('selection', '_selection')).toBe('_selection-2')
  })

  it('keeps a label taken while a queued prompt still uses that chip', () => {
    setComposerSelectionReference('_selection', 'queued quote')
    enqueueQueuedPrompt('queued-session', {
      attachments: [],
      text: '@selection:`_selection` send this later'
    })
    setComposerDraft('')

    expect(nextComposerContextReferenceLabel('selection', '_selection')).toBe('_selection-2')

    reconcileComposerContextReferences('')
    expect(composerContextReferenceText('selection', '_selection')).toBe('queued quote')
  })
})

describe('selection references survive a restart', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $composerContextReferences.set({})
  })

  it('persists the quoted text so a restored draft chip is not a dud', () => {
    setComposerSelectionReference('_selection', 'Selected assistant text')

    const raw = window.localStorage.getItem(CONTEXT_REFS_STORAGE_KEY)

    expect(raw).toBeTruthy()
    expect(JSON.parse(raw as string)).toEqual({ 'selection:_selection': 'Selected assistant text' })
  })

  it('drops the storage key when references are cleared', () => {
    setComposerSelectionReference('_selection', 'Selected assistant text')
    clearComposerContextReferences()
    expect(window.localStorage.getItem(CONTEXT_REFS_STORAGE_KEY)).toBeNull()
  })

  // The regression that made this necessary: draft text is persisted, so the chip
  // came back after a restart while its text did not — a chip that contributes
  // nothing to the prompt and shows nothing on hover. Both keys are seeded here
  // because that is what a real restart restores: the draft AND its references.
  it('rehydrates on module load, keeping hover text and prompt context', async () => {
    window.localStorage.setItem(
      SESSION_DRAFTS_STORAGE_KEY,
      JSON.stringify({ __new__: '@selection:`_selection` go on' })
    )
    window.localStorage.setItem(
      CONTEXT_REFS_STORAGE_KEY,
      JSON.stringify({ 'selection:_selection': 'Text quoted before the restart' })
    )

    vi.resetModules()

    const store = await import('@/store/composer')

    expect(store.composerContextReferenceText('selection', '_selection')).toBe('Text quoted before the restart')
    expect(store.composerContextBlocksFromDraft('@selection:`_selection` go on')).toEqual([
      'Selected context (_selection):\n```text\nText quoted before the restart\n```'
    ])
  })

  it('rehydrates references used only by a persisted queued prompt', async () => {
    window.localStorage.setItem(
      'hermes.desktop.composerQueue.v1',
      JSON.stringify({
        'queued-session': [
          {
            id: 'queued-1',
            text: '@selection:`_selection` send this later',
            attachments: [],
            queuedAt: 1
          }
        ]
      })
    )
    window.localStorage.setItem(
      CONTEXT_REFS_STORAGE_KEY,
      JSON.stringify({ 'selection:_selection': 'Text quoted before queue persistence' })
    )

    vi.resetModules()

    const store = await import('@/store/composer')

    expect(store.composerContextReferenceText('selection', '_selection')).toBe(
      'Text quoted before queue persistence'
    )
    expect(store.nextComposerContextReferenceLabel('selection', '_selection')).toBe('_selection-2')
  })

  // Storage must not grow forever: a reference whose chip was deleted in an
  // earlier session has no draft mentioning it and is dropped on load.
  it('drops references no restored draft mentions', async () => {
    window.localStorage.setItem(SESSION_DRAFTS_STORAGE_KEY, JSON.stringify({ __new__: 'just text' }))
    window.localStorage.setItem(
      CONTEXT_REFS_STORAGE_KEY,
      JSON.stringify({ 'selection:_selection': 'orphaned quote' })
    )

    vi.resetModules()

    const store = await import('@/store/composer')

    expect(store.$composerContextReferences.get()).toEqual({})
    expect(store.nextComposerContextReferenceLabel('selection', '_selection')).toBe('_selection')
  })

  it('ignores a corrupt payload instead of throwing on load', async () => {
    window.localStorage.setItem(CONTEXT_REFS_STORAGE_KEY, '{not json')
    vi.resetModules()

    const store = await import('@/store/composer')

    expect(store.$composerContextReferences.get()).toEqual({})
  })
})
