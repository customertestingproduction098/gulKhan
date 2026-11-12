export const MONITOR_MODES = {
  SHOP: 'shop',
  WAREHOUSE: 'warehouse',
}

export const cx = (...classes) => classes.filter(Boolean).join(' ')

export const formatDate = (value) => {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleDateString(undefined, {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  })
}

export const formatTime = (value) => {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export const timeAgo = (value) => {
  if (!value) return '—'
  const then = new Date(value).getTime()
  if (Number.isNaN(then)) return '—'
  const diff = Date.now() - then
  if (diff < 0) return 'just now'
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d ago`
  return formatDate(value)
}

export const percent = (part, total) => {
  if (!total) return 0
  return Math.round((Number(part) / Number(total)) * 100)
}

export const getApiBase = () =>
  (import.meta.env.VITE_API_URL || 'http://localhost:5000/api').replace(/\/$/, '')

export const getStreamUrl = (channel) =>
  `${getApiBase()}/camera/stream/${channel}?t=${Date.now()}`

export const buildQuery = (params = {}) => {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      search.set(key, value)
    }
  })
  const query = search.toString()
  return query ? `?${query}` : ''
}
