import { queryOptions } from '@tanstack/react-query'

import { getCodexUsage } from '@/hermes'

export const CODEX_PROVIDER = 'openai-codex'
export const CODEX_USAGE_REFRESH_MS = 60_000

export function normalizeCodexUsageProfile(profile: null | string | undefined): string {
  return String(profile ?? '').trim() || 'default'
}

export function normalizeCodexUsageProvider(provider: null | string | undefined): string {
  return String(provider ?? '')
    .trim()
    .toLowerCase()
}

export function codexUsageQueryOptions({
  enabled = true,
  profile,
  provider
}: {
  enabled?: boolean
  profile: null | string | undefined
  provider: null | string | undefined
}) {
  const normalizedProfile = normalizeCodexUsageProfile(profile)
  const normalizedProvider = normalizeCodexUsageProvider(provider)

  return queryOptions({
    enabled: enabled && normalizedProvider === CODEX_PROVIDER,
    queryFn: () => getCodexUsage(normalizedProfile),
    queryKey: ['codex-usage', normalizedProfile, normalizedProvider] as const,
    refetchInterval: CODEX_USAGE_REFRESH_MS,
    refetchOnWindowFocus: false,
    staleTime: CODEX_USAGE_REFRESH_MS
  })
}
