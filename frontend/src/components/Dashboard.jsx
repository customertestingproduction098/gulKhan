import { useCallback, useEffect, useState } from 'react'
import { useDispatch, useSelector } from 'react-redux'
import { toast } from 'react-toastify'
import { entryApi, settingsApi } from '../services/api'
import { acknowledgeAlert, fetchAlerts } from '../store/slices/alertSlice'
import { cx, formatTime, getApiBase, timeAgo } from '../utils/helpers'
import AlertLog from './AlertLog'
import { IconRefresh, IconShield } from './Icons'

const dateLabel = new Date().toLocaleDateString(undefined, {
  weekday: 'long',
  day: '2-digit',
  month: 'long',
  year: 'numeric',
})

const API_ORIGIN = getApiBase().replace(/\/api$/, '')

const snapshotUrl = (entry) => {
  const path = entry.frame_url || entry.snapshot
  if (!path) return null
  return path.startsWith('http') ? path : `${API_ORIGIN}${path}`
}

const isUnknown = (entry) => !entry.authorized && !entry.false_alarm

function StatusTag({ entry }) {
  if (entry.false_alarm) return <span className="tag text-ink-faint">False alarm</span>
  if (entry.authorized) return <span className="tag tag-sea">VIP</span>
  return <span className="tag tag-rust">Unknown</span>
}

export default function Dashboard() {
  const dispatch = useDispatch()
  const alerts = useSelector((state) => state.alerts.items).filter(
    (alert) => alert.title !== 'System Alert' && alert.title !== 'Test alert'
  )
  const alertsLoading = useSelector((state) => state.alerts.loading)
  const owner = useSelector((state) => state.auth.owner)
  const [entries, setEntries] = useState([])
  const [settings, setSettings] = useState(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    dispatch(fetchAlerts({ limit: 50 }))
    try {
      const [entryData, settingData] = await Promise.all([
        entryApi.list({ limit: 25 }),
        settingsApi.get(),
      ])
      setEntries(
        (entryData.entries || []).filter(
          (entry) => entry.reason !== 'Test entry' && entry.reason !== 'System test'
        )
      )
      setSettings(settingData.settings || settingData)
    } catch (error) {
      toast.error(error.friendlyMessage || 'Could not load the warehouse watch')
    } finally {
      setLoading(false)
    }
  }, [dispatch])

  useEffect(() => {
    load()
    const timer = setInterval(() => dispatch(fetchAlerts({ limit: 50 })), 30000)
    return () => clearInterval(timer)
  }, [dispatch, load])

  const warehouse = settings?.warehouse || {}
  const startTime = warehouse.after_hours_start || '21:00'
  const endTime = warehouse.after_hours_end || '11:00'
  const notificationEmail =
    settings?.owner?.notification_email ||
    owner?.notification_email ||
    settings?.owner?.email ||
    owner?.email ||
    'alerts@client.com'
  const afterHours = entries.filter((entry) => entry.after_hours)
  const unknown = afterHours.filter(isUnknown)
  const emailed = entries.filter((entry) => entry.alert_sent)

  const verdict = unknown.length
    ? {
        headline: `${unknown.length} ${unknown.length === 1 ? 'unknown person' : 'unknown people'} after hours.`,
        note: `Each of these was emailed to ${notificationEmail} with the snapshot and timestamp.`,
        stamp: 'Review needed',
        tone: 'stamp-rust',
      }
    : afterHours.length
      ? {
          headline: `${afterHours.length} ${afterHours.length === 1 ? 'entry' : 'entries'} after hours.`,
          note: 'Only VIPs were recognised, so no unknown-person alert was raised.',
          stamp: 'VIP only',
          tone: 'stamp-ochre',
        }
      : {
          headline: 'All quiet after hours.',
          note: `No one entered the warehouse between ${startTime} and ${endTime}.`,
          stamp: 'Checked',
          tone: 'stamp-sea',
        }

  const handleAcknowledge = async (id) => {
    try {
      await dispatch(acknowledgeAlert(id)).unwrap()
      toast.success('Alert acknowledged')
    } catch (error) {
      toast.error(error || 'Could not acknowledge alert')
    }
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="eyebrow">Today · {dateLabel}</p>
          <h1 className="mt-2 font-display text-4xl font-extrabold uppercase leading-none tracking-[0.01em] text-ink sm:text-5xl">
            Warehouse watch
          </h1>
        </div>
        <button type="button" className="btn-secondary" onClick={load} disabled={loading}>
          <IconRefresh width="14" height="14" />
          {loading ? 'Reading…' : 'Refresh'}
        </button>
      </header>

      <section className="sheet overflow-hidden" aria-labelledby="verdict-heading">
        <div className="border-b border-rule p-5 sm:p-7">
          <div className="flex items-start justify-between gap-4">
            <div>
              <p className="eyebrow">After-hours status</p>
              <h2
                id="verdict-heading"
                className="mt-3 font-display text-[34px] font-extrabold uppercase leading-[0.92] tracking-[0.005em] text-ink sm:text-[44px]"
              >
                {verdict.headline}
              </h2>
              <p className="mt-3 max-w-xl text-sm leading-relaxed text-ink-soft">
                {verdict.note}
              </p>
            </div>
            <span className={cx('stamp stamp-enter shrink-0', verdict.tone)}>{verdict.stamp}</span>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-px bg-rule-soft lg:grid-cols-4">
          <div className="bg-sheet p-4 sm:p-5">
            <p className="eyebrow">After-hours window</p>
            <p className="mt-3 font-mono text-[22px] font-bold leading-none tabular-nums text-ochre">
              {startTime}
              <span className="px-1 text-ink-faint">–</span>
              {endTime}
            </p>
          </div>
          <div className="bg-sheet p-4 sm:p-5">
            <p className="eyebrow">Unknown after hours</p>
            <p className={cx('figure mt-3', unknown.length ? 'text-rust' : 'text-ink')}>
              {unknown.length}
            </p>
          </div>
          <div className="bg-sheet p-4 sm:p-5">
            <p className="eyebrow">Emailed to owner</p>
            <p className="figure mt-3">{emailed.length}</p>
          </div>
          <div className="bg-sheet p-4 sm:p-5">
            <p className="eyebrow">Notification inbox</p>
            <p className="mt-3 truncate font-mono text-[12px] text-ink" title={notificationEmail}>
              {notificationEmail}
            </p>
          </div>
        </div>
      </section>

      <div className="grid gap-5 xl:grid-cols-[1.5fr_1fr]">
        <section className="sheet self-start">
          <div className="sheet-head">
            <div className="flex items-center gap-2">
              <IconShield width="15" height="15" className="text-ink-soft" />
              <h2 className="eyebrow">Recent entries</h2>
            </div>
            <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-ink-faint">
              {entries.length} logged
            </span>
          </div>

          <div className="max-h-[32rem] divide-y divide-rule-soft overflow-y-auto">
            {loading && (
              <p className="px-5 py-10 text-center text-sm text-ink-soft">Reading entries…</p>
            )}
            {!loading && entries.length === 0 && (
              <div className="px-5 py-12 text-center">
                <p className="text-sm font-medium text-ink">No entries recorded</p>
                <p className="note mt-1">The warehouse entrance has been quiet.</p>
              </div>
            )}
            {!loading &&
              entries.map((entry) => {
                const shot = snapshotUrl(entry)
                return (
                  <article key={entry.id} className="flex gap-3 px-4 py-3.5 sm:px-5">
                    {shot ? (
                      <a
                        href={shot}
                        target="_blank"
                        rel="noreferrer"
                        className="h-14 w-20 shrink-0 overflow-hidden border border-rule bg-sheet-deep"
                      >
                        <img
                          src={shot}
                          alt={`Snapshot at ${formatTime(entry.timestamp || entry.created_at)}`}
                          className="h-full w-full object-cover"
                          loading="lazy"
                        />
                      </a>
                    ) : (
                      <span className="grid h-14 w-20 shrink-0 place-items-center border border-dashed border-rule font-mono text-[9px] uppercase tracking-[0.08em] text-ink-faint">
                        No image
                      </span>
                    )}

                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                        <span className="font-mono text-[11px] text-ink">
                          {formatTime(entry.timestamp || entry.created_at)}
                        </span>
                        <span className="font-mono text-[10px] uppercase tracking-[0.06em] text-ink-faint">
                          {timeAgo(entry.timestamp || entry.created_at)}
                        </span>
                        <span className="ml-auto flex flex-wrap items-center gap-1.5">
                          {entry.after_hours && <span className="tag tag-ochre">After hours</span>}
                          <StatusTag entry={entry} />
                        </span>
                      </div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1">
                        <span className="font-mono text-[10px] uppercase tracking-[0.06em] text-ink-soft">
                          {entry.person_count != null
                            ? `${entry.person_count} ${entry.person_count === 1 ? 'person' : 'people'}`
                            : 'Count unavailable'}
                        </span>
                        <span className="font-mono text-[10px] uppercase tracking-[0.06em] text-ink-faint">
                          {entry.alert_sent ? 'Emailed to owner' : 'Not emailed'}
                        </span>
                      </div>
                    </div>
                  </article>
                )
              })}
          </div>
        </section>

        <AlertLog
          alerts={alerts}
          loading={alertsLoading}
          limit={5}
          onAcknowledge={handleAcknowledge}
          emptyText="No after-hours alerts."
        />
      </div>
    </div>
  )
}
