/**
 * Instant hover preview for composer reference chips.
 *
 * A `@selection:` chip shows only a placeholder label, so the quoted text has to
 * be revealed on hover. A native `title` cannot do it: Chromium waits 1–2s and
 * suppresses the tip outright after recent pointer movement, so it shows up late
 * or not at all. Chips also live inside a contenteditable as raw DOM, so they
 * cannot be a Radix tooltip trigger.
 *
 * One document-level listener therefore watches for `[data-ref-preview]` and
 * renders a fixed-position panel next to the hovered chip. Styling mirrors
 * TooltipContent's `panel` appearance so it tracks the theme, and it is
 * pointer-events-none so it can never swallow a click meant for the composer.
 */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'

const PREVIEW_MAX_WIDTH = 420
const VIEWPORT_MARGIN = 8
const GAP = 6

interface PreviewState {
  left: number
  text: string
  top: number
}

function chipFrom(target: EventTarget | null): HTMLElement | null {
  if (!(target instanceof Element)) {
    return null
  }

  return target.closest<HTMLElement>('[data-ref-preview]')
}

/** Place above the chip when there is room, else below; clamp horizontally so a
 *  chip near the right edge does not push the panel off-screen. */
function positionFor(chip: HTMLElement, estimatedHeight: number) {
  const rect = chip.getBoundingClientRect()
  const above = rect.top - GAP - estimatedHeight
  const top = above >= VIEWPORT_MARGIN ? above : rect.bottom + GAP
  const maxLeft = window.innerWidth - PREVIEW_MAX_WIDTH - VIEWPORT_MARGIN

  return { left: Math.max(VIEWPORT_MARGIN, Math.min(rect.left, Math.max(VIEWPORT_MARGIN, maxLeft))), top }
}

export function ComposerChipPreview() {
  const [preview, setPreview] = useState<PreviewState | null>(null)

  useEffect(() => {
    const show = (event: Event) => {
      const chip = chipFrom(event.target)
      const text = chip?.dataset.refPreview?.trim()

      if (!chip || !text) {
        return
      }

      // Rough height estimate purely for the above/below decision; the panel is
      // capped by max-height so a long quote cannot cover the window.
      const estimatedHeight = Math.min(240, 32 + Math.ceil(text.length / 60) * 18)

      setPreview({ ...positionFor(chip, estimatedHeight), text })
    }

    const hide = (event: Event) => {
      const chip = chipFrom(event.target)

      if (chip) {
        setPreview(null)
      }
    }

    const dismiss = () => setPreview(null)

    // pointerover/pointerout bubble, so one pair of listeners covers every
    // composer instance (main chat plus session tiles) and any chip re-created
    // by an editor normalize pass.
    document.addEventListener('pointerover', show)
    document.addEventListener('pointerout', hide)
    // The panel is absolutely placed, so anything that moves the chip out from
    // under it (scrolling the composer or thread, losing focus) must dismiss.
    window.addEventListener('scroll', dismiss, true)
    window.addEventListener('blur', dismiss)

    return () => {
      document.removeEventListener('pointerover', show)
      document.removeEventListener('pointerout', hide)
      window.removeEventListener('scroll', dismiss, true)
      window.removeEventListener('blur', dismiss)
    }
  }, [])

  if (!preview) {
    return null
  }

  return createPortal(
    <div
      className="pointer-events-none fixed z-[200] max-h-60 overflow-hidden whitespace-pre-wrap rounded-md border border-(--ui-stroke-secondary) bg-(--ui-chat-surface-background) px-2.5 py-2 text-xs leading-snug text-foreground shadow-md select-none"
      data-slot="composer-chip-preview"
      style={{ left: preview.left, maxWidth: PREVIEW_MAX_WIDTH, top: preview.top }}
    >
      {preview.text}
    </div>,
    document.body
  )
}
