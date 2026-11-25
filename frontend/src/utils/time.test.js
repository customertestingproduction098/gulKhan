import assert from 'node:assert/strict'
import { test } from 'node:test'
import { formatDateTime, parseTimestamp, timeAgo } from './time.js'

const NOW = Date.parse('2026-11-25T04:37:00Z')
const SEC = 1000
const MIN = 60 * SEC
const HOUR = 60 * MIN
const DAY = 24 * HOUR

// ISO strings exactly as the backend serializes them (UTC) and as an Asia/Karachi
// (+05:00) client would see them, for the same instant.
const utcIso = (msAgo) => new Date(NOW - msAgo).toISOString()
const pktIso = (msAgo) => {
  const shifted = new Date(NOW - msAgo + 5 * HOUR).toISOString().replace('Z', '+05:00')
  return shifted
}
const backendIso = (msAgo) => utcIso(msAgo).replace('Z', '+00:00') // Python isoformat() style

const cases = [
  ['10 seconds ago', 10 * SEC, 'Just now'],
  ['59 seconds ago', 59 * SEC, 'Just now'],
  ['5 minutes ago', 5 * MIN, '5m ago'],
  ['59 minutes ago', 59 * MIN, '59m ago'],
  ['3 hours ago', 3 * HOUR, '3h ago'],
  ['23 hours ago', 23 * HOUR, '23h ago'],
  ['1 day ago', 1 * DAY, '1d ago'],
  ['3 days ago', 3 * DAY, '3d ago'],
]

for (const [label, msAgo, expected] of cases) {
  test(`timeAgo: ${label} (Z, +05:00, +00:00 all agree)`, () => {
    assert.equal(timeAgo(utcIso(msAgo), NOW), expected)
    assert.equal(timeAgo(pktIso(msAgo), NOW), expected)
    assert.equal(timeAgo(backendIso(msAgo), NOW), expected)
  })
}

test('alerts created today never render "3d"', () => {
  for (const msAgo of [0, 10 * SEC, 5 * MIN, 2 * HOUR, 12 * HOUR, 23 * HOUR]) {
    for (const iso of [utcIso(msAgo), pktIso(msAgo), backendIso(msAgo)]) {
      const text = timeAgo(iso, NOW)
      assert.doesNotMatch(text, /\d+d ago/, `${iso} -> ${text}`)
      assert.match(text, /^(Just now|\d+m ago|\d+h ago)$/)
    }
  }
})

test('naive ISO strings are treated as UTC, not browser-local time', () => {
  const naive = new Date(NOW - 5 * MIN).toISOString().replace('Z', '')
  assert.equal(parseTimestamp(naive).getTime(), NOW - 5 * MIN)
  assert.equal(timeAgo(naive, NOW), '5m ago')
})

test('future timestamps (clock skew) and invalid input are safe', () => {
  assert.equal(timeAgo(utcIso(-30 * SEC), NOW), 'Just now')
  assert.equal(timeAgo(null, NOW), '—')
  assert.equal(timeAgo('garbage', NOW), '—')
  assert.equal(timeAgo(undefined, NOW), '—')
})

test('7+ day old timestamps fall back to a date, not "Nd ago"', () => {
  assert.match(timeAgo(utcIso(10 * DAY), NOW), /^\d{2} \w{3} 2026$/)
})

test('formatDateTime renders the same local second for Z and +05:00 inputs', () => {
  const a = formatDateTime('2026-11-21T05:28:39Z')
  const b = formatDateTime('2026-11-21T10:28:39+05:00')
  assert.equal(a, b)
})
