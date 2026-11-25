export const MONITOR_MODES = {
  SHOP: 'shop',
  WAREHOUSE: 'warehouse',
}

export const cx = (...classes) => classes.filter(Boolean).join(' ')

export const percent = (part, total) => {
  if (!total) return 0
  return Math.round((Number(part) / Number(total)) * 100)
}

const DEFAULT_API_BASE = import.meta.env.PROD
  ? 'https://gulkhan-production.up.railway.app/api'
  : 'http://localhost:5000/api'

export const getApiBase = () =>
  (import.meta.env.VITE_API_URL || DEFAULT_API_BASE).replace(/\/$/, '')

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
