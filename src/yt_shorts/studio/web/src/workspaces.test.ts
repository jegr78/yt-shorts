import { describe, expect, it } from 'vitest'
import { isValidWorkspaceName, joinPath } from './workspaces'

describe('isValidWorkspaceName', () => {
  it.each(['erf', 'my-ws', 'ws.2', 'A_1'])('accepts %s', (n) => {
    expect(isValidWorkspaceName(n)).toBe(true)
  })
  it.each(['', '.hidden', '../x', 'a/b', 'a b', 'ws\n', 'ws\r'])('rejects %s', (n) => {
    expect(isValidWorkspaceName(n)).toBe(false)
  })
})

describe('joinPath', () => {
  it('joins parent and name', () => {
    expect(joinPath('/a/b', 'c')).toBe('/a/b/c')
    expect(joinPath('/a/b/', 'c')).toBe('/a/b/c')
    expect(joinPath('/', 'c')).toBe('/c')
  })
})
