import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ComposerChipPreview } from './chip-preview'
import { RICH_INPUT_SLOT } from './rich-editor'

const PANEL = '[data-slot="composer-chip-preview"]'

/** A composer input holding one previewable chip, laid out like the real one:
 *  the panel anchors to the input, so the input must have a rect. */
function mountComposer(text: string) {
  const input = document.createElement('div')

  input.dataset.slot = RICH_INPUT_SLOT
  input.getBoundingClientRect = () =>
    ({ bottom: 700, height: 60, left: 40, right: 640, top: 640, width: 600, x: 40, y: 640 }) as DOMRect

  const chip = document.createElement('span')

  chip.dataset.refText = '@selection:`_selection-1`'
  chip.dataset.refPreview = text
  input.append(chip)
  document.body.append(input)

  return { chip, input }
}

const hover = (el: Element) => act(() => void el.dispatchEvent(new Event('pointerover', { bubbles: true })))
const panel = () => document.querySelector(PANEL)

describe('ComposerChipPreview', () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }))

  afterEach(() => {
    vi.useRealTimers()
    cleanup()
    document.body.innerHTML = ''
  })

  it('shows the quoted text when a chip is hovered', () => {
    const { chip } = mountComposer('quoted line')

    render(<ComposerChipPreview />)
    hover(chip)

    expect(screen.getByText('quoted line')).toBeTruthy()
  })

  // The reported bug: the panel guessed its own height to decide above-or-below,
  // and a quote with newlines is far taller than its character count implies, so
  // it reached back down over the chip and buried the remove button. Anchoring to
  // the composer and growing upward from `bottom` removes the guess entirely.
  it('anchors above the composer, never overlapping the chip row', () => {
    const { chip } = mountComposer('many\nshort\nlines\nof\nquoted\ntext')

    render(<ComposerChipPreview />)
    hover(chip)

    const style = (panel() as HTMLElement).style

    // window.innerHeight (768 in jsdom) - input top (640) + gap (8)
    expect(style.bottom).toBe('136px')
    expect(style.top).toBe('')
    expect(style.left).toBe('40px')
    expect(style.maxWidth).toBe('600px')
  })

  // Removing a chip while it is hovered — clicking its own × — deletes the
  // element from under the cursor, so no pointerout is ever dispatched and the
  // panel used to hang around until some other chip was hovered.
  it('drops the panel when the hovered chip leaves the document', () => {
    const { chip } = mountComposer('quoted line')

    render(<ComposerChipPreview />)
    hover(chip)
    expect(panel()).not.toBeNull()

    chip.remove()
    act(() => void vi.advanceTimersByTime(300))

    expect(panel()).toBeNull()
  })

  it('dismisses on a press anywhere', () => {
    const { chip } = mountComposer('quoted line')

    render(<ComposerChipPreview />)
    hover(chip)
    expect(panel()).not.toBeNull()

    act(() => void document.body.dispatchEvent(new Event('pointerdown', { bubbles: true })))

    expect(panel()).toBeNull()
  })

  it('dismisses when the pointer moves onto something that is not a chip', () => {
    const { chip, input } = mountComposer('quoted line')

    render(<ComposerChipPreview />)
    hover(chip)
    expect(panel()).not.toBeNull()

    hover(input)

    expect(panel()).toBeNull()
  })

  it('ignores a chip carrying no preview text', () => {
    const bare = document.createElement('span')

    bare.dataset.refText = '@file:`src/main.ts`'
    document.body.append(bare)

    render(<ComposerChipPreview />)
    hover(bare)

    expect(panel()).toBeNull()
  })
})
