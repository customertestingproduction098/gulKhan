import { useCallback, useEffect, useState } from 'react'
import { useDispatch, useSelector } from 'react-redux'
import { toast } from 'react-toastify'
import { alertApi } from '../services/api'
import { acknowledgeAlert, fetchAlerts } from '../store/slices/alertSlice'
import { cx, formatDate, formatTime, timeAgo } from '../utils/helpers'
import { IconAlert, IconCheck, IconRefresh } from '../components/Icons'

const severityTag = {
  critical: 'tag tag-rust',
  warning: 'tag tag-ochre',
  info: 'tag',
}

const FILTERS = [
  { value: '', label: 'All severities' },
  { value: 'critical', label: 'Critical' },
  { value: 'warning', label: 'Warning' },
  { value: 'info', label: 'Info' },
]

export default function Alerts() {
  const dispatch = useDispatch()
  const { items = [], loading, error } = useSelector((state) => state.alerts)
  const [severity, setSeverity] = useState('')
  const [pendingOnly, setPendingOnly] = useState(false)
  const [selected, setSelected] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)

  const load = useCallback(() => {
    const params = { limit: 100 }
    if (severity) params.severity = severity
    if (pendingOnly) params.status = 'pending'
    dispatch(fetchAlerts(params))
  }, [dispatch, pendingOnly, severity])

  useEffect(() => {
    load()
  }, [load])

  const handleAcknowledge = async (alert) => {
    try {
      await dispatch(acknowledgeAlert(alert.id)).unwrap()
      toast.success('Alert acknowledged')
      if (selected?.id === alert.id) {
        setSelected({ ...selected, acknowledged: true, status: 'acknowledged' })
      }
      load()
    } catch (err) {
      toast.error(err || 'Could not acknowledge alert')
    }
  }

  const openDetail = async (alert) => {
    setSelected(alert)
    setDetailLoading(true)
    try {
      const data = await alertApi.get(alert.id)
      setSelected(data.alert || data)
    } catch (err) {
      toast.error(err.friendlyMessage || 'Could not load alert detail')
    } finally {
      setDetailLoading(false)
    }
  }

  const pending = items.filter((alert) => !alert.acknowledged && alert.status !== 'acknowledged')
  const critical = items.filter((alert) => alert.severity === 'critical').length
  const selectedAcknowledged = selected && (selected.acknowledged || selected.status === 'acknowledged')

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="mt-2 font-display text-4xl font-extrabold uppercase leading-none tracking-[0.01em] text-ink sm:text-5xl">
            Alerts
          </h1>
        </div>
        <button type="button" onClick={load} className="btn-secondary" disabled={loading}>
          <IconRefresh width="14" height="14" /> Refresh
        </button>
      </header>

      <div className="sheet grid grid-cols-3 gap-px bg-rule-soft">
        <div className="bg-sheet p-4 sm:p-5">
          <p className="eyebrow">Pending</p>
          <p className="figure mt-3">{pending.length}</p>
        </div>
        <div className="bg-sheet p-4 sm:p-5">
          <p className="eyebrow">Critical</p>
          <p className="figure mt-3 text-rust">{critical}</p>
        </div>
        <div className="bg-sheet p-4 sm:p-5">
          <p className="eyebrow">On file</p>
          <p className="figure mt-3">{items.length}</p>
        </div>
      </div>

      <div className="sheet flex flex-wrap items-center gap-x-5 gap-y-3 px-4 py-3 sm:px-5">
        <select
          className="input w-auto py-1.5 font-mono text-[11px]"
          value={severity}
          onChange={(event) => setSeverity(event.target.value)}
          aria-label="Filter by severity"
        >
          {FILTERS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <label className="flex cursor-pointer items-center gap-2 font-mono text-[10px] uppercase tracking-[0.08em] text-ink-soft">
          <input
            type="checkbox"
            checked={pendingOnly}
            onChange={(event) => setPendingOnly(event.target.checked)}
            className="h-3.5 w-3.5"
          />
          Pending only
        </label>
        {error && <span className="text-sm text-rust">{error}</span>}
        <span className="ml-auto font-mono text-[10px] uppercase tracking-[0.1em] text-ink-faint">
          {items.length} loaded
        </span>
      </div>

      <div className="grid gap-5 xl:grid-cols-5">
        <section className="sheet xl:col-span-2">
          <div className="sheet-head">
            <h2 className="eyebrow">Feed</h2>
            <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-ink-faint">
              Newest first
            </span>
          </div>
          <div className="max-h-[38rem] divide-y divide-rule-soft overflow-y-auto">
            {loading && (
              <p className="px-5 py-8 text-center text-sm text-ink-soft">Reading alerts…</p>
            )}
            {!loading && items.length === 0 && (
              <div className="px-5 py-10 text-center">
                <p className="text-sm font-medium text-ink">No alerts match these filters.</p>
                <p className="note mt-1">Clear the severity or pending filter to see the full log.</p>
              </div>
            )}
            {items.filter(alert => alert.title !== 'System Alert' && alert.title !== 'Test alert').map((alert) => {
              const acknowledged = alert.acknowledged || alert.status === 'acknowledged'
              const isSelected = selected?.id === alert.id
              return (
                <button
                  key={alert.id}
                  type="button"
                  onClick={() => openDetail(alert)}
                  className={cx(
                    'block w-full border-l-2 px-4 py-3.5 text-left transition-colors sm:px-5',
                    isSelected
                      ? 'border-rust bg-sheet-deep'
                      : 'border-transparent hover:border-rule hover:bg-sheet-deep/60',
                  )}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={severityTag[alert.severity] || severityTag.info}>
                      {alert.severity || 'info'}
                    </span>
                    <span
                      className={cx(
                        'font-mono text-[10px] uppercase tracking-[0.08em]',
                        acknowledged ? 'text-sea' : 'text-rust',
                      )}
                    >
                      {acknowledged ? 'Acknowledged' : 'Pending'}
                    </span>
                    <span className="ml-auto font-mono text-[10px] text-ink-faint">
                      {timeAgo(alert.created_at)}
                    </span>
                  </div>
                  <p className="mt-1.5 truncate text-sm font-semibold text-ink">{alert.title}</p>
                  <p className="mt-0.5 line-clamp-2 text-xs leading-relaxed text-ink-soft">
                    {alert.message}
                  </p>
                </button>
              )
            })}
          </div>
        </section>

        <section className="sheet xl:col-span-3">
          {!selected ? (
            <div className="grid h-full min-h-[24rem] place-items-center px-6 py-12 text-center">
              <div>
                <IconAlert className="mx-auto text-ink-faint" width="26" height="26" />
                <p className="mt-3 text-sm font-medium text-ink">Open an alert from the feed.</p>
                <p className="note mt-1">Its evidence, escalation trail, and controls appear here.</p>
              </div>
            </div>
          ) : (
            <div className="sheet-pad">
              <div className="flex flex-wrap items-start justify-between gap-4 border-b border-rule pb-4">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={severityTag[selected.severity] || severityTag.info}>
                      {selected.severity || 'info'}
                    </span>
                    <span className="tag">
                      {(selected.alert_type || selected.type || 'alert').replace(/_/g, ' ')}
                    </span>
                    {selected.channel != null && (
                      <span className="tag">CH {String(selected.channel).padStart(2, '0')}</span>
                    )}
                  </div>
                  <h2 className="mt-3 font-display text-2xl font-bold uppercase leading-tight tracking-[0.01em] text-ink">
                    {selected.title}
                  </h2>
                  <p className="mt-1.5 text-sm leading-relaxed text-ink-soft">{selected.message}</p>
                </div>
                <span
                  className={cx(
                    'stamp shrink-0',
                    selectedAcknowledged ? 'stamp-sea' : 'stamp-rust',
                  )}
                >
                  {selectedAcknowledged ? 'Acknowledged' : 'Pending'}
                </span>
              </div>

              <dl className="mt-4 divide-y divide-rule-soft">
                <div className="ledger py-2.5">
                  <dt className="eyebrow">Detected</dt>
                  <span className="leader" aria-hidden="true" />
                  <dd className="font-mono text-[11px] text-ink">
                    {formatDate(selected.created_at)} · {formatTime(selected.created_at)}
                  </dd>
                </div>
                <div className="ledger py-2.5">
                  <dt className="eyebrow">Status</dt>
                  <span className="leader" aria-hidden="true" />
                  <dd className="font-mono text-[11px] uppercase tracking-[0.06em] text-ink">
                    {selected.status || (selectedAcknowledged ? 'acknowledged' : 'pending')}
                  </dd>
                </div>
                <div className="ledger py-2.5">
                  <dt className="eyebrow">Channels sent</dt>
                  <span className="leader" aria-hidden="true" />
                  <dd className="text-right font-mono text-[11px] text-ink">
                    {Array.isArray(selected.sent_channels) && selected.sent_channels.length
                      ? selected.sent_channels.join(', ')
                      : '—'}
                  </dd>
                </div>
                <div className="ledger py-2.5">
                  <dt className="eyebrow">Escalations</dt>
                  <span className="leader" aria-hidden="true" />
                  <dd className="text-right font-mono text-[11px] text-ink">
                    {Array.isArray(selected.escalations) && selected.escalations.length
                      ? selected.escalations.map((step) => step.channel || step.level).join(', ')
                      : 'None'}
                  </dd>
                </div>
              </dl>

              {!selectedAcknowledged && (
                <button
                  type="button"
                  onClick={() => handleAcknowledge(selected)}
                  className="btn-primary mt-4"
                >
                  <IconCheck width="14" height="14" /> Acknowledge alert
                </button>
              )}

              {selected.snapshot_url && (
                <div className="mt-5">
                  <p className="eyebrow mb-2">Snapshot</p>
                  <img
                    src={selected.snapshot_url}
                    alt="Alert snapshot"
                    className="w-full border border-rule object-cover"
                  />
                </div>
              )}

              {selected.metadata && Object.keys(selected.metadata).length > 0 && (
                <div className="mt-5">
                  <p className="eyebrow mb-2">Recorded detail</p>
                  <pre className="max-h-48 overflow-auto border border-rule bg-sheet-deep p-3 font-mono text-[11px] leading-relaxed text-ink-soft">
                    {JSON.stringify(selected.metadata, null, 2)}
                  </pre>
                </div>
              )}
              {detailLoading && (
                <p className="mt-3 font-mono text-[10px] uppercase tracking-[0.1em] text-ink-faint">
                  Reading detail…
                </p>
              )}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
