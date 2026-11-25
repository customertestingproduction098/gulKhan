// Single source of truth for timestamp parsing, display, and relative time.
// Pure module (no React / Vite imports) so it can be unit-tested with `node --test`.

const DISPLAY_TIMEZONE = import.meta.env?.VITE_APP_TIMEZONE || undefined

const HAS_TIMEZONE = /(?:[zZ]|[+-]\d{2}:?\d{2})$/

// Parse an API timestamp into a Date. The backend always sends an explicit offset
// (UTC, "+00:00"), but a naive ISO string would otherwise be read as *browser-local*
// time and silently shift the instant, so naive strings are treated as UTC.
export const parseTimestamp = (value) => {
  if (value === null || value === undefined || value === '') return null
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  let input = value
  if (typeof input === 'string') {
    input = input.trim()
    if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(input) && !HAS_TIMEZONE.test(input)) {
      input = `${input.replace(' ', 'T')}Z`
    }
  }
  const date = new Date(input)
  return Number.isNaN(date.getTime()) ? null : date
}

export const formatDate = (value) => {
  const date = parseTimestamp(value)
  if (!date) return '—'
  return date.toLocaleDateString('en-GB', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    timeZone: DISPLAY_TIMEZONE,
  })
}

export const formatTime = (value) => {
  const date = parseTimestamp(value)
  if (!date) return '—'
  return date.toLocaleTimeString('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
    timeZone: DISPLAY_TIMEZONE,
  })
}

export const formatDateTime = (value) => {
  const date = parseTimestamp(value)
  if (!date) return '—'
  return `${formatDate(date)}, ${formatTime(date)}`
}

// Relative time. `now` is injectable (ms since epoch) so tests are deterministic.
export const timeAgo = (value, now = Date.now()) => {
  const date = parseTimestamp(value)
  if (!date) return '—'
  const diff = now - date.getTime()
  if (diff < 60_000) return 'Just now' // includes small client/server clock skew (diff < 0)
  const minutes = Math.floor(diff / 60_000)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d ago`
  return formatDate(date)
}
