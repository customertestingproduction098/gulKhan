import { useEffect, useRef, useState } from 'react'
import { getApiBase, MONITOR_MODES } from '../utils/helpers'
import { IconCamera, IconPlay, IconStop } from './Icons'

const DEFAULT_ALLOWED = [2, 4, 5, 7, 8, 9, 11]

export default function CameraViewer({
  channel = 2,
  channels: providedChannels,
  mode = MONITOR_MODES.WAREHOUSE,
  monitoring = false,
  loading = false,
  onChannelChange,
  onStart,
  onStop,
  title = 'Live camera',
  subtitle,
}) {
  const [streamError, setStreamError] = useState(false)
  const canvasRef = useRef(null)

  const channels =
    providedChannels && providedChannels.length > 0
      ? providedChannels
      : DEFAULT_ALLOWED
  const modeLabel = mode === MONITOR_MODES.WAREHOUSE ? 'After-hours watch' : 'Greeting check'

  useEffect(() => {
    let isMounted = true
    const abortController = new AbortController()
    setStreamError(false)

    const SOI = new Uint8Array([0xff, 0xd8]) // Start of Image marker
    const EOI = new Uint8Array([0xff, 0xd9]) // End of Image marker

    const findMarker = (buffer, marker, start = 0) => {
      const len = buffer.length - 1
      for (let i = start; i < len; i++) {
        if (buffer[i] === marker[0] && buffer[i + 1] === marker[1]) {
          return i
        }
      }
      return -1
    }

    const startStream = async () => {
      try {
        const streamUrl = `${getApiBase()}/camera/stream/${channel}`
        const response = await fetch(streamUrl, {
          signal: abortController.signal,
          cache: 'no-store',
        })

        if (!response.ok || !response.body) {
          throw new Error(`HTTP ${response.status}`)
        }

        const reader = response.body.getReader()
        let chunks = []
        let totalLen = 0

        while (isMounted) {
          const { value, done } = await reader.read()
          if (done) break
          if (!value) continue

          chunks.push(value)
          totalLen += value.length

          // Combine accumulated chunks into a single Uint8Array
          const buffer = new Uint8Array(totalLen)
          let offset = 0
          for (const chunk of chunks) {
            buffer.set(chunk, offset)
            offset += chunk.length
          }

          let searchOffset = 0
          let lastFrameEnd = -1

          while (true) {
            const soi = findMarker(buffer, SOI, searchOffset)
            if (soi === -1) break

            const eoi = findMarker(buffer, EOI, soi + 2)
            if (eoi === -1) break

            const frameEnd = eoi + 2
            const jpegBytes = buffer.subarray(soi, frameEnd)

            const blob = new Blob([jpegBytes], { type: 'image/jpeg' })
            createImageBitmap(blob)
              .then((bitmap) => {
                if (!isMounted || !canvasRef.current) {
                  bitmap.close()
                  return
                }
                const canvas = canvasRef.current
                if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
                  canvas.width = bitmap.width
                  canvas.height = bitmap.height
                }
                const ctx = canvas.getContext('2d')
                if (ctx) {
                  ctx.drawImage(bitmap, 0, 0)
                }
                bitmap.close()
              })
              .catch(() => {})

            lastFrameEnd = frameEnd
            searchOffset = frameEnd
          }

          if (lastFrameEnd !== -1) {
            const remainder = buffer.subarray(lastFrameEnd)
            chunks = remainder.length > 0 ? [remainder] : []
            totalLen = remainder.length
          }
        }
      } catch (err) {
        if (err.name !== 'AbortError' && isMounted) {
          setStreamError(true)
        }
      }
    }

    startStream()

    return () => {
      isMounted = false
      abortController.abort() // Immediately cuts the HTTP/TCP connection on channel change
    }
  }, [channel])

  return (
    <section className="sheet overflow-hidden">
      <div className="sheet-head">
        <div className="flex min-w-0 items-center gap-3">
          <span className="grid h-8 w-8 shrink-0 place-items-center border border-rule text-ink-soft">
            <IconCamera width="16" height="16" />
          </span>
          <div className="min-w-0">
            <p className="eyebrow">
              {monitoring
                ? `Viewing CH ${String(channel).padStart(2, '0')} · Monitoring all ${channels.length} cameras (${modeLabel})`
                : `Channel ${String(channel).padStart(2, '0')} · ${modeLabel}`}
            </p>
            <h2 className="mt-1 truncate text-sm font-semibold text-ink">
              {subtitle || title}
            </h2>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <select
            value={channel}
            onChange={(event) => onChannelChange?.(Number(event.target.value))}
            className="input w-auto py-1.5 font-mono text-[11px]"
            aria-label="Camera channel"
          >
            {channels.map((value) => (
              <option key={value} value={value}>
                CH {String(value).padStart(2, '0')}
              </option>
            ))}
          </select>
          {monitoring ? (
            <button type="button" className="btn-danger py-1.5" onClick={onStop} disabled={loading}>
              <IconStop width="14" height="14" /> Stop all
            </button>
          ) : (
            <button type="button" className="btn-primary py-1.5" onClick={onStart} disabled={loading}>
              <IconPlay width="14" height="14" /> Start all
            </button>
          )}
        </div>
      </div>

      <div className="relative aspect-video w-full bg-sheet-pure">
        {streamError ? (
          <div className="grid h-full w-full place-items-center p-6 text-center">
            <div className="space-y-2">
              <span className="inline-flex border border-[#3A3D34] px-2 py-1 font-mono text-[10px] uppercase tracking-[0.16em] text-[#C9CDBF]">
                No signal · CH {String(channel).padStart(2, '0')}
              </span>
              <span className="block text-sm font-medium text-[#E8EADF]">
                Camera {channel} is not reachable
              </span>
              <p className="text-xs text-[#9B9E8F]">
                Check RTSP credentials or camera connection in Settings.
              </p>
            </div>
          </div>
        ) : (
          <canvas
            ref={canvasRef}
            className="h-full w-full object-contain"
          />
        )}
      </div>
    </section>
  )
}
