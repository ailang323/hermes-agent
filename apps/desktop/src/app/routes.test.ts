import { describe, expect, it } from 'vitest'

import { appViewForPath, contributedRoutes, KANBAN_ROUTE, routeSessionId } from './routes'
import { registry } from '@/contrib/registry'

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
