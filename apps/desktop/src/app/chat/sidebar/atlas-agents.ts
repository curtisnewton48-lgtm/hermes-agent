import { profileColor } from '@/lib/profile-color'
import type { ProfileInfo } from '@/types/hermes'

export interface AtlasAgentSummary {
  accent: string
  detail: string
  initials: string
  name: string
  status: 'Active' | 'Available' | 'Disconnected'
}

function initialsForName(name: string): string {
  const parts = name
    .trim()
    .split(/[-_\s]+/)
    .filter(Boolean)

  if (parts.length === 0) {
    return 'H'
  }

  return parts
    .slice(0, 2)
    .map(part => part[0]?.toUpperCase() ?? '')
    .join('')
}

function detailForProfile(profile: ProfileInfo): string {
  if (profile.provider && profile.model) {
    return `${profile.provider} / ${profile.model}`
  }

  if (profile.model) {
    return profile.model
  }

  if (profile.provider) {
    return profile.provider
  }

  if (profile.skill_count > 0) {
    return `${profile.skill_count} ${profile.skill_count === 1 ? 'skill' : 'skills'}`
  }

  return profile.is_default ? 'General assistant' : 'Configured profile'
}

export function atlasAgentsFromProfiles(
  profiles: readonly ProfileInfo[],
  activeProfile: string,
  limit = 4
): AtlasAgentSummary[] {
  const source =
    profiles.length > 0
      ? profiles
      : [
          {
            has_env: false,
            is_default: activeProfile === 'default',
            model: null,
            name: activeProfile || 'default',
            path: '',
            provider: null,
            skill_count: 0
          }
        ]

  return source.slice(0, limit).map(profile => ({
    accent: profileColor(profile.name) ?? '#2f6df6',
    detail: detailForProfile(profile),
    initials: initialsForName(profile.name),
    name: profile.name,
    status: !profile.has_env ? 'Disconnected' : profile.name === activeProfile ? 'Active' : 'Available'
  }))
}
