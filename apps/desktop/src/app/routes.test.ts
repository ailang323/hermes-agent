import { describe, expect, it } from 'vitest'

import {
  appViewForPath,
  contributedRoutes,
  KANBAN_ROUTE,
  NEW_CHAT_ROUTE,
  primaryRouteSelectedSessionId,
  routeSessionId,
  sessionRoute,
  SETTINGS_ROUTE
} from './routes'
import { registry } from '@/contrib/registry'

const SESS_A = 'sess-a'
const SESS_B = 'sess-b'

describe('primaryRouteSelectedSessionId', () => {
  it('prefers the routed session id over a stale/different store selection (#59305)', () => {
    // The route already committed to B while the store selection hasn't
    // caught up yet (still reads A) — the route wins.
    expect(primaryRouteSelectedSessionId(sessionRoute(SESS_B), SESS_A)).toBe(SESS_B)
  })

  it('returns null on the new-chat route even with a leftover selection from the previous chat', () => {
    expect(primaryRouteSelectedSessionId(NEW_CHAT_ROUTE, SESS_A)).toBeNull()
  })

  it('falls back to the store selection on a non-chat route (settings, overlays)', () => {
    expect(primaryRouteSelectedSessionId(SETTINGS_ROUTE, SESS_A)).toBe(SESS_A)
  })

  it('falls back to the store selection when the route matches the same session', () => {
    expect(primaryRouteSelectedSessionId(sessionRoute(SESS_A), SESS_A)).toBe(SESS_A)
  })

  it('returns null on a non-chat route with no store selection', () => {
    expect(primaryRouteSelectedSessionId(SETTINGS_ROUTE, null)).toBeNull()
  })
})

describe('desktop routes', () => {
  it('reserves /kanban against session-id parsing even before the plugin registers', () => {
    // The Kanban dashboard plugin owns /kanban via a ROUTES_AREA contribution,
    // but the path must never parse as a session id — including on first paint
    // while the plugin is still loading (no contribution registered yet).
    expect(KANBAN_ROUTE).toBe('/kanban')
    expect(routeSessionId('/kanban')).toBeNull()
  })

  it('lets the plugin contribution render /kanban instead of filtering it as reserved', () => {
    const unregister = registry.registerMany([
      {
        id: 'route',
        area: 'routes',
        title: '看板',
        data: { path: '/kanban' },
        render: () => null
      }
    ])
    try {
      const paths = contributedRoutes().map(route => route.path)
      expect(paths).toContain('/kanban')
      expect(appViewForPath('/kanban')).toBe('extension')
    } finally {
      unregister()
    }
  })
})
