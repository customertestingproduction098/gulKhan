import { useEffect, useMemo, useState } from 'react'
import { useDispatch, useSelector } from 'react-redux'
import { toast } from 'react-toastify'
import { cameraApi, settingsApi } from '../services/api'
import { fetchCameraStatus, startMonitoring, stopMonitoring } from '../store/slices/cameraSlice'
import { cx } from '../utils/helpers'
import { IconCamera, IconPlay, IconRefresh, IconStop } from '../components/Icons'

export default function Cameras() {
  const dispatch = useDispatch()
  const status = useSelector((state) => state.camera.status)
  const loading = useSelector((state) => state.camera.loading)
  const [cameras, setCameras] = useState([])
  const [selected, setSelected] = useState(null)
  const [mode, setMode] = useState('shop')
  const [tick, setTick] = useState(Date.now())

  useEffect(() => {
    settingsApi
      .get()
      .then((data) => {
        const list = (data.settings || data).cameras || []
        setCameras(list)
        const firstEnabled = list.find((camera) => camera.enabled)
        setSelected(firstEnabled?.channel ?? list[0]?.channel ?? 2)
      })
      .catch((error) => toast.error(error.friendlyMessage || 'Unable to load cameras'))
    dispatch(fetchCameraStatus())
  }, [dispatch])

  useEffect(() => {
    const timer = setInterval(() => setTick(Date.now()), 5000)
    const statusTimer = setInterval(() => dispatch(fetchCameraStatus()), 20000)
    return () => {
      clearInterval(timer)
      clearInterval(statusTimer)
    }
  }, [dispatch])

  const monitoring = Boolean(status?.monitoring)
  const activeChannel = status?.channel
  const enabled = useMemo(() => cameras.filter((camera) => camera.enabled), [cameras])

  const start = async () => {
    try {
      await dispatch(startMonitoring({ channel: selected, mode })).unwrap()
      toast.success(`Monitoring channel ${selected}`)
      dispatch(fetchCameraStatus())
    } catch (error) {
      toast.error(error || 'Unable to start monitoring')
    }
  }

  const stop = async () => {
    try {
      await dispatch(stopMonitoring()).unwrap()
      toast.info('Monitoring stopped')
    } catch (error) {
      toast.error(error || 'Unable to stop monitoring')
    }
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="label">Live surveillance</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight text-white sm:text-3xl">
            Cameras
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            {enabled.length} enabled of {cameras.length} configured channels.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select className="input max-w-[180px]" value={mode} onChange={(event) => setMode(event.target.value)}>
            <option value="shop">Shop mode</option>
            <option value="warehouse">Warehouse mode</option>
          </select>
          {monitoring ? (
            <button type="button" onClick={stop} className="btn-danger" disabled={loading}>
              <IconStop width="16" height="16" /> Stop monitoring
            </button>
          ) : (
            <button type="button" onClick={start} className="btn-primary" disabled={loading || !selected}>
              <IconPlay width="16" height="16" /> Start channel {selected}
            </button>
          )}
        </div>
      </header>

      <div className="grid gap-6 xl:grid-cols-3">
        <section className="card overflow-hidden xl:col-span-2">
          <div className="flex items-center justify-between border-b border-white/10 px-5 py-3">
            <div className="flex items-center gap-2">
              <IconCamera width="18" height="18" className="text-slate-400" />
              <h2 className="text-sm font-semibold text-white">
                Channel {selected ?? '—'}
              </h2>
              {monitoring && activeChannel === selected && (
                <span className="pill bg-emerald-500/10 text-emerald-300">processing</span>
              )}
            </div>
            <span className="text-xs text-slate-500">Live MJPEG</span>
          </div>
          <div className="aspect-video bg-black">
            {selected && (
              <img
                key={`live-${selected}`}
                src={cameraApi.streamUrl(selected)}
                alt={`Camera ${selected}`}
                className="h-full w-full object-contain"
              />
            )}
          </div>
        </section>

        <section className="card card-pad space-y-4">
          <div>
            <p className="label">Processor</p>
            <p className="mt-1 text-lg font-semibold text-white">
              {monitoring ? `Channel ${activeChannel}` : 'Idle'}
            </p>
            <p className="mt-1 text-xs text-slate-500">
              {monitoring ? status?.mode || 'monitoring' : 'Start a channel to analyze frames.'}
            </p>
          </div>
          <dl className="space-y-3 text-sm">
            <div className="flex items-center justify-between">
              <dt className="text-slate-400">Persons</dt>
              <dd className="font-medium text-slate-100">{status?.person_count ?? 0}</dd>
            </div>
            <div className="flex items-center justify-between">
              <dt className="text-slate-400">Detector</dt>
              <dd className="font-medium text-slate-100">{status?.detection_source || '—'}</dd>
            </div>
            <div className="flex items-center justify-between">
              <dt className="text-slate-400">Shopkeeper</dt>
              <dd className="font-medium text-slate-100">{status?.shopkeeper_present ? 'present' : 'away'}</dd>
            </div>
            <div className="flex items-center justify-between">
              <dt className="text-slate-400">After hours</dt>
              <dd className="font-medium text-slate-100">{status?.after_hours?.active ? 'yes' : 'no'}</dd>
            </div>
          </dl>
          <button
            type="button"
            onClick={() => dispatch(fetchCameraStatus())}
            className="btn-secondary w-full"
          >
            <IconRefresh width="16" height="16" /> Refresh status
          </button>
        </section>
      </div>

      <section className="card card-pad">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">Camera grid</h2>
          <span className="text-xs text-slate-500">Snapshots refresh every 5s</span>
        </div>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {enabled.map((camera) => (
            <button
              key={camera.channel}
              type="button"
              onClick={() => setSelected(camera.channel)}
              className={cx(
                'overflow-hidden rounded-xl border text-left transition-colors',
                selected === camera.channel
                  ? 'border-emerald-400/60 ring-2 ring-emerald-400/20'
                  : 'border-white/10 hover:border-white/25',
              )}
            >
              <div className="aspect-video bg-black">
                <img
                  src={`${cameraApi.snapshotUrl(camera.channel)}&v=${tick}`}
                  alt={camera.name}
                  className="h-full w-full object-cover"
                  loading="lazy"
                />
              </div>
              <div className="flex items-center justify-between px-3 py-2">
                <div>
                  <p className="text-xs font-medium text-slate-200">{camera.name}</p>
                  <p className="text-[11px] text-slate-500">Channel {camera.channel}</p>
                </div>
                {activeChannel === camera.channel && monitoring && (
                  <span className="h-2 w-2 rounded-full bg-emerald-400" />
                )}
              </div>
            </button>
          ))}
          {enabled.length === 0 && (
            <p className="col-span-full py-8 text-center text-sm text-slate-500">
              No enabled cameras. Add or enable channels in Settings.
            </p>
          )}
        </div>
      </section>
    </div>
  )
}
