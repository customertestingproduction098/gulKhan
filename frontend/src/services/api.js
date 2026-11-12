import axios from 'axios'
import { buildQuery, getApiBase } from '../utils/helpers'

const api = axios.create({
  baseURL: getApiBase(),
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    const message =
      error?.response?.data?.error ||
      error?.response?.data?.message ||
      error?.message ||
      'Unexpected API error'
    return Promise.reject(Object.assign(error, { friendlyMessage: message }))
  },
)

export const authApi = {
  login: (payload) => api.post('/auth/login', payload).then((r) => r.data),
}

export const healthApi = {
  check: () => api.get('/health').then((r) => r.data),
}

export const cameraApi = {
  status: () => api.get('/camera/status').then((r) => r.data),
  channelStatus: (channel) => api.get(`/camera/${channel}/status`).then((r) => r.data),
  startMonitor: (payload) => api.post('/camera/monitor/start', payload).then((r) => r.data),
  stopMonitor: () => api.post('/camera/monitor/stop').then((r) => r.data),
  startChannel: (channel, mode) =>
    api.post(`/camera/${channel}/process`, { mode }).then((r) => r.data),
  inspect: (channel) => api.post(`/camera/${channel}/inspect`).then((r) => r.data),
  snapshotUrl: (channel) => `${getApiBase()}/camera/snapshot/${channel}?t=${Date.now()}`,
  streamUrl: (channel) => `${getApiBase()}/camera/stream/${channel}`,
}

export const entryApi = {
  list: (params) => api.get(`/entries${buildQuery(params)}`).then((r) => r.data),
  stats: (params) => api.get(`/entries/stats${buildQuery(params)}`).then((r) => r.data),
  get: (id) => api.get(`/entries/${id}`).then((r) => r.data),
  markFalseAlarm: (id, value = true) =>
    api.post(`/entries/${id}/false-alarm`, { false_alarm: value }).then((r) => r.data),
}

export const alertApi = {
  list: (params) => api.get(`/alerts${buildQuery(params)}`).then((r) => r.data),
  pending: (params) => api.get(`/alerts/pending${buildQuery(params)}`).then((r) => r.data),
  get: (id) => api.get(`/alerts/${id}`).then((r) => r.data),
  acknowledge: (id) => api.post(`/alerts/${id}/acknowledge`).then((r) => r.data),
  sendAlert: (payload) => api.post('/alerts/send', payload).then((r) => r.data),
}

export const reportApi = {
  summary: (params) => api.get(`/reports/summary${buildQuery(params)}`).then((r) => r.data),
  daily: (params) => api.get(`/reports/daily${buildQuery(params)}`).then((r) => r.data),
  weekly: (params) => api.get(`/reports/weekly${buildQuery(params)}`).then((r) => r.data),
  monthly: (params) => api.get(`/reports/monthly${buildQuery(params)}`).then((r) => r.data),
  history: (params) => api.get(`/reports/history${buildQuery(params)}`).then((r) => r.data),
  send: (payload) => api.post('/reports/send', payload).then((r) => r.data),
  csvUrl: (params) => `${getApiBase()}/reports/export/csv${buildQuery(params)}`,
  pdfUrl: (params) => `${getApiBase()}/reports/export/pdf${buildQuery(params)}`,
}

export const settingsApi = {
  get: () => api.get('/settings').then((r) => r.data),
  update: (payload) => api.put('/settings', payload).then((r) => r.data),
}

export const eventsApi = {
  status: () => api.get('/events/status').then((r) => r.data),
  streamUrl: () => `${getApiBase()}/events`,
}

// Compatibility aliases for older pages.
export const logApi = {
  interactions: (params) =>
    api.get(`/logs/interactions${buildQuery(params)}`).then((r) => r.data),
  entries: (params) => api.get(`/logs/entries${buildQuery(params)}`).then((r) => r.data),
  reportSummary: (params) =>
    api.get(`/logs/reports/summary${buildQuery(params)}`).then((r) => r.data),
  reportCsvUrl: (params) =>
    `${getApiBase()}/logs/reports/export${buildQuery(params)}`,
  sendReport: (payload) =>
    api.post('/logs/reports/send', payload).then((r) => r.data),
}

export default api
