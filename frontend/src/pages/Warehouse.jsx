import { useCallback, useEffect, useMemo, useState } from 'react'
import { useDispatch, useSelector } from 'react-redux'
import { toast } from 'react-toastify'
import AlertLog from '../components/AlertLog'
import CameraViewer from '../components/CameraViewer'
import { IconShield } from '../components/Icons'
import { entryApi } from '../services/api'
import { acknowledgeAlert, fetchAlerts } from '../store/slices/alertSlice'
import {
  fetchCameraStatus,
  selectChannel,
  startMonitoring,
  stopMonitoring,
} from '../store/slices/cameraSlice'
import { MONITOR_MODES, cx, formatTime, timeAgo } from '../utils/helpers'

function Verdict({ entry }) {
  if (entry.false_alarm) return <span className="tag text-ink-faint">False alarm</span>
  if (entry.authorized) return <span className="tag tag-sea">Known person</span>
  return <span className="tag tag-rust">Unauthorized</span>
}

export default function Warehouse() {
  const dispatch = useDispatch()
  const { selectedChannel, loading, status } = useSelector((state) => state.camera)
  const warehouseCameras = useMemo(
    () => (status?.allowed_channels?.length ? status.allowed_channels : [2, 4, 5, 7, 8, 9, 11]),
    [status],
  )
  const alertItems = useSelector((state) => state.alerts.items)
  const alerts = useMemo(
    () => (alertItems || []).filter(
      (alert) => alert.title !== 'System Alert' && alert.title !== 'Test alert'
    ),
    [alertItems],
  )
  const alertsLoading = useSelector((state) => state.alerts.loading)
  const [entries, setEntries] = useState([])
  const [stats, setStats] = useState(null)
  const [afterHoursOnly, setAfterHoursOnly] = useState(false)
  const [fetching, setFetching] = useState(false)

  const monitoring = Boolean(status?.monitoring && status?.mode === MONITOR_MODES.WAREHOUSE)

  const loadEntries = useCallback(async () => {
    setFetching(true)
    try {
      const params = { limit: 100 }
      if (afterHoursOnly) params.after_hours = true
      const [data, statData] = await Promise.all([
        entryApi.list(params),
        entryApi.stats({ days: 7 }),
      ])
      const filteredEntries = (data.entries || []).filter(
        (entry) =>
          entry.reason !== 'Test entry' &&
          entry.reason !== 'System test' &&
          entry.authorized === false
      )
      setEntries(filteredEntries)
      setStats(statData)
    } catch (error) {
      toast.error(error.friendlyMessage || 'Could not load warehouse entries')
    } finally {
      setFetching(false)
    }
  }, [afterHoursOnly])

  // Initial fetch for camera status and alerts on mount
  useEffect(() => {
    dispatch(fetchCameraStatus())
    dispatch(fetchAlerts({ limit: 50 }))
  }, [dispatch])

  // Load entries on mount and whenever afterHoursOnly filter changes
  useEffect(() => {
    loadEntries()
  }, [loadEntries])

  // Ensure selected channel is valid whenever available cameras change
  useEffect(() => {
    if (warehouseCameras.length > 0 && !warehouseCameras.includes(selectedChannel)) {
      dispatch(selectChannel(warehouseCameras[0]))
    }
  }, [dispatch, selectedChannel, warehouseCameras])

  const derived = useMemo(() => {
    const unauthorized = entries.filter((entry) => !entry.authorized).length
    const afterHours = entries.filter((entry) => entry.after_hours).length
    const falseAlarms = entries.filter((entry) => entry.false_alarm).length
    return {
      total: stats?.total_entries ?? stats?.total ?? entries.length,
      unauthorized: stats?.unauthorized_entries ?? stats?.unauthorized ?? unauthorized,
      afterHours: stats?.after_hours_entries ?? stats?.after_hours ?? afterHours,
      falseAlarms: stats?.false_alarms ?? stats?.false_alarm_entries ?? falseAlarms,
    }
  }, [entries, stats])

  const handleStart = async () => {
    try {
      const result = await dispatch(
        startMonitoring({ channel: selectedChannel, mode: MONITOR_MODES.WAREHOUSE }),
      ).unwrap()
      const activeList = (result?.active_channels || warehouseCameras)
        .map((ch) => String(ch).padStart(2, '0'))
        .join(', ')
      toast.success(`After-hours watch active on all cameras (${activeList})`)
    } catch (error) {
      toast.error(error || 'Could not start monitoring')
    }
  }

  const handleStop = async () => {
    try {
      await dispatch(stopMonitoring()).unwrap()
      toast.info('Warehouse monitoring stopped on all cameras')
    } catch (error) {
      toast.error(error || 'Could not stop monitoring')
    }
  }

  const handleAcknowledge = async (id) => {
    try {
      await dispatch(acknowledgeAlert(id)).unwrap()
      toast.success('Alert acknowledged')
    } catch (error) {
      toast.error(error || 'Could not acknowledge alert')
    }
  }


  const markFalseAlarm = async (entry) => {
    try {
      await entryApi.markFalseAlarm(entry.id, !entry.false_alarm)
      toast.success(entry.false_alarm ? 'False alarm cleared' : 'Marked as false alarm')
      loadEntries()
    } catch (error) {
      toast.error(error.friendlyMessage || 'Could not update entry')
    }
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="eyebrow">Post 02 · Warehouse</p>
          <h1 className="mt-2 font-display text-4xl font-extrabold uppercase leading-none tracking-[0.01em] text-ink sm:text-5xl">
            After-hours watch
          </h1>
          <p className="mt-3 max-w-2xl text-sm leading-relaxed text-ink-soft">
            Anyone detected outside working hours is recorded and emailed to you. Unacknowledged
            alerts escalate to your second contact.
          </p>
        </div>
      </header>

      <div className="sheet grid grid-cols-2 gap-px bg-rule-soft lg:grid-cols-4">
        <div className="bg-sheet p-4 sm:p-5">
          <p className="eyebrow">After-hours window</p>
          <p className="mt-3 font-mono text-[22px] font-bold leading-none tabular-nums text-ochre">
            {status?.after_hours?.start ?? '21:00'}
            <span className="px-1 text-ink-faint">–</span>
            {status?.after_hours?.end ?? '11:00'}
          </p>
          <p className="note mt-3">
            {status?.after_hours?.active ? 'Inside the window now' : 'Outside the window now'}
          </p>
        </div>
        <div className="bg-sheet p-4 sm:p-5">
          <p className="eyebrow">Entries logged</p>
          <p className="figure mt-3">{derived.total}</p>
          <p className="note mt-3">{derived.afterHours} after hours</p>
        </div>
        <div className="bg-sheet p-4 sm:p-5">
          <p className="eyebrow">Unauthorized</p>
          <p className="figure mt-3 text-rust">{derived.unauthorized}</p>
          <p className="note mt-3">Awaiting review</p>
        </div>
        <div className="bg-sheet p-4 sm:p-5">
          <p className="eyebrow">False alarms</p>
          <p className="figure mt-3">{derived.falseAlarms}</p>
          <p className="note mt-3">Marked by you</p>
        </div>
      </div>

      <CameraViewer
        channel={selectedChannel}
        channels={warehouseCameras}
        mode={MONITOR_MODES.WAREHOUSE}
        monitoring={monitoring}
        loading={loading}
        onChannelChange={(channel) => dispatch(selectChannel(channel))}
        onStart={handleStart}
        onStop={handleStop}
        title="Warehouse entrance camera"
      />

      <div className="grid gap-5 xl:grid-cols-2">
        <section className="sheet">
          <div className="sheet-head">
            <div className="flex items-center gap-2">
              <IconShield width="15" height="15" className="text-ink-soft" />
              <h2 className="eyebrow">Entry log</h2>
            </div>
            <div className="flex items-center gap-3">
              <label className="flex cursor-pointer items-center gap-2 font-mono text-[10px] uppercase tracking-[0.08em] text-ink-soft">
                <input
                  type="checkbox"
                  checked={afterHoursOnly}
                  onChange={(event) => setAfterHoursOnly(event.target.checked)}
                  className="h-3.5 w-3.5"
                />
                After hours only
              </label>
              <button
                type="button"
                className="font-mono text-[10px] uppercase tracking-[0.1em] text-rust underline decoration-rust/40 underline-offset-2 hover:text-rust-deep"
                onClick={loadEntries}
              >
                Refresh
              </button>
            </div>
          </div>

          <div className="max-h-[34rem] divide-y divide-rule-soft overflow-y-auto">
            {fetching && (
              <p className="px-5 py-10 text-center text-sm text-ink-soft">Reading entries…</p>
            )}
            {!fetching && entries.length === 0 && (
              <div className="px-5 py-12 text-center">
                <p className="text-sm font-medium text-ink">No entries recorded</p>
                <p className="note mt-1">The warehouse has been empty in this range.</p>
              </div>
            )}
            {!fetching &&
              entries.map((entry) => (
                <article key={entry.id} className="px-4 py-3.5 sm:px-5">
                  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                    <span className="font-mono text-[11px] text-ink">
                      {formatTime(entry.timestamp || entry.created_at)}
                    </span>
                    <span className="font-mono text-[10px] uppercase tracking-[0.06em] text-ink-faint">
                      {timeAgo(entry.timestamp || entry.created_at)} · CH{' '}
                      {entry.channel != null ? String(entry.channel).padStart(2, '0') : '—'}
                    </span>
                    <span className="ml-auto flex flex-wrap items-center gap-1.5">
                      {entry.after_hours && <span className="tag tag-ochre">After hours</span>}
                      <Verdict entry={entry} />
                    </span>
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 pl-0">
                    <span className="font-mono text-[10px] uppercase tracking-[0.06em] text-ink-soft">
                      {entry.person_count != null
                        ? `${entry.person_count} ${entry.person_count === 1 ? 'person' : 'people'}`
                        : 'Count unavailable'}
                    </span>
                    {entry.snapshot_url && (
                      <a
                        href={entry.snapshot_url}
                        target="_blank"
                        rel="noreferrer"
                        className="font-mono text-[10px] uppercase tracking-[0.1em] text-rust underline decoration-rust/40 underline-offset-2 hover:text-rust-deep"
                      >
                        Open snapshot
                      </a>
                    )}
                    <button
                      type="button"
                      onClick={() => markFalseAlarm(entry)}
                      className={cx(
                        'font-mono text-[10px] uppercase tracking-[0.1em] text-ink-faint',
                        'underline decoration-rule underline-offset-2 transition-colors hover:text-ink',
                      )}
                    >
                      {entry.false_alarm ? 'Clear false alarm' : 'Mark false alarm'}
                    </button>
                    {entry.reason && (
                      <span className="font-mono text-[10px] text-ink-faint">{entry.reason}</span>
                    )}
                  </div>
                </article>
              ))}
          </div>
        </section>

        <AlertLog
          alerts={alerts}
          loading={alertsLoading}
          onAcknowledge={handleAcknowledge}
          emptyText="No security alerts."
        />
      </div>
    </div>
  )
}
