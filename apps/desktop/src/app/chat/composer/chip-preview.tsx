/**
 * Instant hover preview for composer reference chips.
 *
 * A `@selection:` chip shows only a placeholder label, so the quoted text has to
 * be revealed on hover. A native `title` cannot do it: Chromium waits 1–2s and
 * suppresses the tip outright after recent pointer movement, so it shows up late
 * or not at all. Chips also live inside a contenteditable as raw DOM, so they
 * cannot be a Radix tooltip trigger — hence one document-level listener and a
 * portalled panel.
 *
 * The panel is anchored ABOVE THE COMPOSER, not beside the chip. Floating it by
 * the chip meant guessing its height to decide above-or-below, and the guess is
 * unknowable: a quote with many short lines is far taller than its character
 * count suggests, so the panel reached back down over the chip and buried the
 * remove button — worst exactly when the quote was long enough to want checking.
 * Anchoring to the composer and growing upward from `bottom` needs no estimate
 * at all.
 */
import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'

import { RICH_INPUT_SLOT } from './rich-editor'

const VIEWPORT_MARGIN = 8
const GAP = 8

interface PreviewState {
  /** The chip being previewed — held so the panel can be dropped the moment it
   *  leaves the document, which `pointerout` never reports. */
  chip: HTMLElement
  left: number
  bottom: number
  maxWidth: number
  text: string
}

function chipFrom(target: EventTarget | null): HTMLElement | null {
  if (!(target instanceof Element)) {
    return null
  }

  return target.closest<HTMLElement>('[data-ref-preview]')
}

/** Sit directly above the composer that owns this chip, matching its width. */
function anchorFor(chip: HTMLElement) {
  const input = chip.closest(`[data-slot="${RICH_INPUT_SLOT}"]`) ?? document.querySelector(`[data-slot="${RICH_INPUT_SLOT}"]`)
  const rect = (input ?? chip).getBoundingClientRect()

  return {
    bottom: Math.max(VIEWPORT_MARGIN, window.innerHeight - rect.top + GAP),
    left: Math.max(VIEWPORT_MARGIN, rect.left),
    maxWidth: Math.max(160, rect.width)
  }
}

export function ComposerChipPreview() {
  const [preview, setPreview] = useState<PreviewState | null>(null)
  const dismiss = useCallback(() => setPreview(null), [])

  useEffect(() => {
    // One handler for both directions. pointerout alone cannot close this: when a
    // chip is removed while hovered — clicking its own × — the element vanishes
    // from under the cursor and no leave event is ever dispatched, so the panel
    // stayed up until some other chip was hovered.
    const sync = (event: Event) => {
      const chip = chipFrom(event.target)
      const text = chip?.dataset.refPreview?.trim()

      if (!chip || !text) {
        setPreview(current => (current ? null : current))

        return
      }

      setPreview({ ...anchorFor(chip), chip, text })
    }

    document.addEventListener('pointerover', sync)
    // Any press dismisses — including the one landing on the remove button.
    document.addEventListener('pointerdown', dismiss, true)
    window.addEventListener('scroll', dismiss, true)
    window.addEventListener('blur', dismiss)

    return () => {
      document.removeEventListener('pointerover', sync)
      document.removeEventListener('pointerdown', dismiss, true)
      window.removeEventListener('scroll', dismiss, true)
      window.removeEventListener('blur', dismiss)
    }
  }, [dismiss])

  // Backstop for the case no pointer event can report: the chip was removed, or
  // the whole draft was replaced, while the panel was open.
  useEffect(() => {
    if (!preview) {
      return
    }

    const check = window.setInterval(() => {
      if (!preview.chip.isConnected) {
        dismiss()
      }
    }, 250)

    return () => window.clearInterval(check)
  }, [dismiss, preview])

  if (!preview) {
    return null
  }

  return createPortal(
    <div
      className="pointer-events-none fixed z-[200] max-h-60 overflow-hidden whitespace-pre-wrap rounded-md border border-(--ui-stroke-secondary) bg-(--ui-chat-surface-background) px-2.5 py-2 text-xs leading-snug text-foreground shadow-md select-none"
      data-slot="composer-chip-preview"
      style={{ bottom: preview.bottom, left: preview.left, maxWidth: preview.maxWidth }}
    >
      {preview.text}
    </div>,
    document.body
  )
}
