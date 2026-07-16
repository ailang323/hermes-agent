'use client'

import { useCallback, useEffect, useRef, useState } from 'react'

import { requestComposerInsert } from '@/app/chat/composer/focus'
import { type BrowserSelectionMatch, currentThreadSelection } from '@/app/chat/selection-reference'
import { isAddSelectionShortcut } from '@/app/right-sidebar/terminal/selection'
import { formatRefValue } from '@/components/assistant-ui/directive-text'
import { Tip } from '@/components/ui/tooltip'
import { triggerHaptic } from '@/lib/haptics'
import { cn } from '@/lib/utils'
import { nextComposerContextReferenceLabel, setComposerSelectionReference } from '@/store/composer'

const HIDE_DELAY_MS = 300
const BUTTON_OFFSET = 8

function QuoteGlyph() {
  return (
    <svg aria-hidden="true" className="size-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
      <path d="M6 17h3a3 3 0 0 0 3-3v-3a4 4 0 0 0-4-4H6v5h4" />
      <path d="M14 17h3a3 3 0 0 0 3-3v-3a4 4 0 0 0-4-4h-2v5h4" />
    </svg>
  )
}

export function FloatingQuoteButton() {
  const [visible, setVisible] = useState(false)
  const [position, setPosition] = useState({ top: 0, left: 0 })
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const label = navigator.language.toLowerCase().startsWith('zh') ? '引用所选内容' : 'Quote selection'

  const clearHideTimer = useCallback(() => {
    if (hideTimerRef.current) {
      clearTimeout(hideTimerRef.current)
      hideTimerRef.current = null
    }
  }, [])

  const scheduleHide = useCallback(() => {
    clearHideTimer()
    hideTimerRef.current = setTimeout(() => setVisible(false), HIDE_DELAY_MS)
  }, [clearHideTimer])

  const quoteSelection = useCallback((match: BrowserSelectionMatch) => {
    const refLabel = nextComposerContextReferenceLabel('selection', '_selection')
    setComposerSelectionReference(refLabel, match.text)
    requestComposerInsert(`@selection:${formatRefValue(refLabel)}`, { mode: 'inline', target: 'main' })
    triggerHaptic('selection')
    match.selection.removeAllRanges()
    setVisible(false)
  }, [])

  const handleSelectionChange = useCallback(() => {
    const match = currentThreadSelection()

    if (!match) {
      scheduleHide()

      return
    }

    clearHideTimer()
    const rect = match.range.getBoundingClientRect()
    const viewportRect = match.viewport.getBoundingClientRect()
    const top = rect.top - viewportRect.top - 32 - BUTTON_OFFSET
    const left = rect.left + rect.width / 2 - viewportRect.left - 16
    setPosition({ top: Math.max(top, 4), left: Math.max(left, 4) })
    setVisible(true)
  }, [clearHideTimer, scheduleHide])

  const handleQuote = useCallback(() => {
    const match = currentThreadSelection()

    if (match) {quoteSelection(match)}
  }, [quoteSelection])

  const handleKeyDown = useCallback(
    (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setVisible(false)

        return
      }

      if (!isAddSelectionShortcut(event)) {return}
      const match = currentThreadSelection()

      if (!match) {return}
      event.preventDefault()
      event.stopPropagation()
      quoteSelection(match)
    },
    [quoteSelection]
  )

  useEffect(() => {
    document.addEventListener('selectionchange', handleSelectionChange)
    document.addEventListener('keydown', handleKeyDown)

    return () => {
      document.removeEventListener('selectionchange', handleSelectionChange)
      document.removeEventListener('keydown', handleKeyDown)
      clearHideTimer()
    }
  }, [clearHideTimer, handleKeyDown, handleSelectionChange])

  if (!visible) {return null}

  return (
    <Tip label={label} side="top">
      <button
        aria-label={label}
        className={cn(
          'pointer-events-auto absolute z-50 flex h-8 w-8 items-center justify-center rounded-md',
          'bg-popover text-muted-foreground shadow-lg ring-1 ring-(--ui-stroke-primary)',
          'transition-all duration-150 hover:scale-110 hover:bg-(--ui-row-hover-background) hover:text-foreground active:scale-95'
        )}
        onClick={handleQuote}
        style={{ top: `${position.top}px`, left: `${position.left}px` }}
        type="button"
      >
        <QuoteGlyph />
      </button>
    </Tip>
  )
}
