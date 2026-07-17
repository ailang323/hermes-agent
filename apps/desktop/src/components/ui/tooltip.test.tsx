import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { suppressNonKeyboardFocusOpen, Tip, TipHintLabel } from './tooltip'

// Radix opens tooltips on ANY trigger focus; menus/dialogs restore focus to
// their trigger on close, which left tips stuck open after a mouse pick (e.g.
// the composer model pill). The trigger's focus handler must preventDefault —
// which Radix's composed handler honors — for non-keyboard focus only.

const focusEvent = (matchesImpl: (selector: string) => boolean) => {
  const preventDefault = vi.fn()

  return {
    event: {
      currentTarget: { matches: matchesImpl } as unknown as HTMLElement,
      preventDefault
    } as unknown as React.FocusEvent<HTMLElement>,
    preventDefault
  }
}

describe('suppressNonKeyboardFocusOpen', () => {
  it('suppresses the focus-open when focus is not keyboard-visible (menu close restore)', () => {
    const { event, preventDefault } = focusEvent(selector => selector !== ':focus-visible')

    suppressNonKeyboardFocusOpen(event)

    expect(preventDefault).toHaveBeenCalledOnce()
  })

  it('keeps the focus-open for keyboard (Tab) focus — a11y path', () => {
    const { event, preventDefault } = focusEvent(selector => selector === ':focus-visible')

    suppressNonKeyboardFocusOpen(event)

    expect(preventDefault).not.toHaveBeenCalled()
  })

  it('fails open when :focus-visible is unsupported', () => {
    const { event, preventDefault } = focusEvent(() => {
      throw new Error('unsupported selector')
    })

    suppressNonKeyboardFocusOpen(event)

    expect(preventDefault).not.toHaveBeenCalled()
  })
})

describe('Tip', () => {
  afterEach(() => {
    cleanup()
  })

  it('shows on pointer enter and dismisses on pointer leave', async () => {
    render(
      <Tip label="Layout editor — ⌘-click resets the layout">
        <button type="button">layout</button>
      </Tip>
    )

    const trigger = screen.getByRole('button', { name: 'layout' })

    fireEvent.pointerMove(trigger, { pointerType: 'mouse' })
    expect((await screen.findByRole('tooltip')).textContent).toContain('Layout editor — ⌘-click resets the layout')

    fireEvent.pointerLeave(trigger)
    await waitFor(() => {
      expect(screen.queryByRole('tooltip')).toBeNull()
    })
  })

  it('renders the child alone when label is empty', () => {
    render(
      <Tip label="">
        <button type="button">bare</button>
      </Tip>
    )

    expect(screen.getByRole('button', { name: 'bare' })).toBeTruthy()
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('guards a block-level label child via the decoration wrapper class', async () => {
    render(
      <Tip label={<span className="flex items-center gap-2">broken label</span>}>
        <button type="button">trigger</button>
      </Tip>
    )

    fireEvent.pointerMove(screen.getByRole('button', { name: 'trigger' }), { pointerType: 'mouse' })
    await screen.findByRole('tooltip')

    // jsdom applies no real Tailwind, so assert the guarding class is present on
    // the decoration wrapper — that's what forces any direct child inline-flex
    // in a browser (#62022).
    const decoration = document.querySelector<HTMLElement>('[data-slot="tooltip-content"]')?.firstElementChild

    expect(decoration?.className).toMatch(/\[&>\*\]:!inline-flex/)
  })

  it('renders rich content as a real panel instead of the inline marker decoration', async () => {
    render(
      <Tip appearance="panel" label={<div>quota details</div>}>
        <button type="button">model</button>
      </Tip>
    )

    fireEvent.pointerMove(screen.getByRole('button', { name: 'model' }), { pointerType: 'mouse' })
    await screen.findByRole('tooltip')

    const panel = document.querySelector<HTMLElement>('[data-slot="tooltip-panel"]')
    const content = document.querySelector<HTMLElement>('[data-slot="tooltip-content"]')

    expect(panel).toBeTruthy()
    expect(panel?.style.width).toBe('100%')
    expect(content?.style.width).toBe('18rem')
    expect(content?.style.maxWidth).toBe('calc(100vw - 1rem)')
    expect(document.querySelector('[data-slot="tooltip-decoration"]')).toBeNull()
  })
})

describe('TipHintLabel', () => {
  afterEach(() => {
    cleanup()
  })

  it('renders inline-flex with a hint and plain text without one', () => {
    const { rerender } = render(<TipHintLabel hint="Ctrl+`" text="PowerShell" />)
    const withHint = screen.getByText('PowerShell').parentElement

    expect(withHint?.classList.contains('inline-flex')).toBe(true)
    expect(withHint?.classList.contains('flex')).toBe(false)

    rerender(<TipHintLabel text="PowerShell" />)
    expect(screen.getByText('PowerShell').tagName).not.toBe('SPAN')
  })
})
