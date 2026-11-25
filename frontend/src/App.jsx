import { useCallback, useEffect } from 'react'
import { useDispatch, useSelector } from 'react-redux'
import { Navigate, Route, Routes } from 'react-router-dom'
import { toast } from 'react-toastify'
import Dashboard from './components/Dashboard'
import Navigation from './components/Navigation'
import SignIn from './components/SignIn'
import useEventStream from './hooks/useEventStream'
import Alerts from './pages/Alerts'
import Settings from './pages/Settings'
import Warehouse from './pages/Warehouse'
import { fetchAlerts, upsertAlert } from './store/slices/alertSlice'
import { checkBackendHealth, fetchCameraStatus } from './store/slices/cameraSlice'

export default function App() {
  const dispatch = useDispatch()
  const isAuthenticated = useSelector((state) => state.auth.isAuthenticated)
  const health = useSelector((state) => state.camera.health)

  const handleEvent = useCallback(
    (type, data) => {
      if (type === 'alert.created') {
        const alert = data?.alert || data
        if (alert?.id) {
          dispatch(upsertAlert(alert))
        }
        dispatch(fetchAlerts())
        if (alert?.severity === 'critical') {
          const cameraLabel =
            alert?.metadata?.camera_label ||
            (alert?.channel != null ? `CH ${String(alert.channel).padStart(2, '0')}` : '')
          const title = alert.title || 'Critical security alert'
          const toastText =
            cameraLabel && !title.includes(cameraLabel) ? `${title} (${cameraLabel})` : title
          toast.warn(toastText, {
            toastId: `alert-${alert.id || alert.channel || 'all'}`,
            autoClose: 5000,
          })
        }
      }
      if (type === 'alert.updated' || type === 'alert.acknowledged') {
        const alert = data?.alert || data
        if (alert?.id) {
          dispatch(upsertAlert(alert))
        }
        dispatch(fetchAlerts())
      }
      if (type === 'entry.created' || type === 'monitoring.started' || type === 'monitoring.stopped') {
        dispatch(fetchCameraStatus())
      }
      if (type === 'settings.updated') {
        dispatch(fetchCameraStatus())
      }
    },
    [dispatch],
  )

  const liveConnected = useEventStream({ enabled: isAuthenticated, onEvent: handleEvent })

  useEffect(() => {
    if (!isAuthenticated) return undefined
    dispatch(checkBackendHealth())
    dispatch(fetchAlerts())
    dispatch(fetchCameraStatus())

    const healthTimer = setInterval(() => dispatch(checkBackendHealth()), 45000)
    const cameraTimer = setInterval(() => dispatch(fetchCameraStatus()), 30000)
    const alertTimer = setInterval(() => dispatch(fetchAlerts()), 30000)

    return () => {
      clearInterval(healthTimer)
      clearInterval(cameraTimer)
      clearInterval(alertTimer)
    }
  }, [dispatch, isAuthenticated])

  if (!isAuthenticated) {
    return <SignIn />
  }

  return (
    <div className="min-h-screen lg:pl-60">
      <Navigation
        backendOnline={health?.status === 'ok'}
        liveConnected={liveConnected}
      />
      <main className="mx-auto w-full max-w-[1180px] px-4 py-6 sm:px-6 lg:px-8">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/warehouse" element={<Warehouse />} />
          <Route path="/alerts" element={<Alerts />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}
