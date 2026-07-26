import { describe, expect, it } from 'vitest'

import { atlasAgentsFromProfiles } from './atlas-agents'

describe('atlasAgentsFromProfiles', () => {
  it('summarizes configured profiles without inventing live status', () => {
    const agents = atlasAgentsFromProfiles(
      [
        {
          has_env: true,
          is_default: true,
          model: 'gpt-5',
          name: 'default',
          path: '/tmp/default',
          provider: 'openai',
          skill_count: 2
        },
        {
          has_env: false,
          is_default: false,
          model: null,
          name: 'Code Reviewer',
          path: '/tmp/reviewer',
          provider: null,
          skill_count: 7
        }
      ],
      'default'
    )

    expect(agents).toEqual([
      {
        accent: expect.any(String),
        detail: 'openai / gpt-5',
        initials: 'D',
        name: 'default',
        status: 'Active'
      },
      {
        accent: expect.any(String),
        detail: '7 skills',
        initials: 'CR',
        name: 'Code Reviewer',
        status: 'Disconnected'
      }
    ])
  })

  it('labels configured inactive profiles as available', () => {
    expect(
      atlasAgentsFromProfiles(
        [
          {
            has_env: true,
            is_default: false,
            model: 'review-model',
            name: 'reviewer',
            path: '/tmp/reviewer',
            provider: 'openai',
            skill_count: 0
          }
        ],
        'default'
      )[0]?.status
    ).toBe('Available')
  })

  it('falls back to the active profile while the profile list is unavailable', () => {
    expect(atlasAgentsFromProfiles([], 'default')).toEqual([
      {
        accent: expect.any(String),
        detail: 'General assistant',
        initials: 'D',
        name: 'default',
        status: 'Disconnected'
      }
    ])
  })
})
