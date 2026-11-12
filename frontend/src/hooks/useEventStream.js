import { useEffect, useRef, useState } from 'react'
import { eventsApi } from '../services/api'

const EVENT_TYPES = [
  'interaction.created',
  'entry.created',
  'entry.updated',
  'alert.created',
  'alert.acknowledged',
  'alert.escalated',
  'settings.updated',
  'monitoring.started',
  'monitoring.stopped',
]

export default function useEventStream({ enabled = true, onEvent } = {}) {
  const [connected, setConnected] = useState(false)
  const handlerRef = useRef(onEvent)

  useEffect(() => {
    handlerRef.current = onEvent
  }, [onEvent])

  useEffect(() => {
    if (!enabled) return undefined

    const source = new EventSource(eventsApi.streamUrl())

    const handle = (event) => {
      let data = {}
      try {
        data = event.data ? JSON.parse(event.data) : {}
      } catch {
        data = { raw: event.data }
      }
      handlerRef.current?.(event.type, data)
    }

    source.onopen = () => setConnected(true)
    source.onerror = () => setConnected(false)
    EVENT_TYPES.forEach((type) => source.addEventListener(type, handle))

    return () => {
      EVENT_TYPES.forEach((type) => source.removeEventListener(type, handle))
      source.close()
      setConnected(false)
    }
  }, [enabled])

  return connected
}
