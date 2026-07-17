import { ContribBoundary } from './boundary'
import { useContributions } from './use-contributions'

import type { ContributionRenderContext } from '../types'

export interface SlotProps {
  /** Area id whose contributions render inline, in order. */
  area: string
  /** State scoped to this rendered surface (for example, a tile's model). */
  context?: ContributionRenderContext
}

/** Renders a bar area: ordered inline items `[...core, ...plugin]`. */
export function Slot({ area, context }: SlotProps) {
  const items = useContributions(area)

  if (items.length === 0) {
    return null
  }

  return (
    <>
      {items.map(c => (
        <ContribBoundary id={c.id} key={`${c.source ?? 'core'}:${c.id}`} variant="chip">
          {c.render?.(context)}
        </ContribBoundary>
      ))}
    </>
  )
}
