import { cx, formatTime, timeAgo } from '../utils/helpers'
import { IconAlert, IconCheck } from './Icons'

const severityTag = {
  critical: 'tag tag-rust',
  warning: 'tag tag-ochre',
  info: 'tag',
}

const severityLabel = {
  critical: 'Critical',
  warning: 'Warning',
  info: 'Info',
}

export default function AlertLog({
  alerts = [],
  loading = false,
  limit,
  onAcknowledge,
  emptyText = 'No alerts on file.',
  showHeader = true,
}) {
  const visible = limit ? alerts.slice(0, limit) : alerts

  return (
    <section className="sheet">
      {showHeader && (
        <div className="sheet-head">
          <div className="flex items-center gap-2">
            <IconAlert width="15" height="15" className="text-ink-soft" />
            <h2 className="eyebrow">Latest from the log</h2>
          </div>
          <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-ink-faint">
            {visible.length} of {alerts.length}
          </span>
        </div>
      )}

      <div className="rule-list">
        {loading && (
          <p className="px-4 py-8 text-center text-sm text-ink-soft sm:px-5">Reading the log…</p>
        )}

        {!loading && visible.length === 0 && (
          <div className="px-5 py-10 text-center">
            <span className="mx-auto grid h-9 w-9 place-items-center border border-sea text-sea">
              <IconCheck width="16" height="16" />
            </span>
            <p className="mt-3 text-sm font-medium text-ink">{emptyText}</p>
            <p className="note mt-1">Critical detections land here and in your inbox.</p>
          </div>
        )}

        {!loading &&
          visible.map((alert) => {
            const acknowledged = alert.acknowledged || alert.status === 'acknowledged'
            return (
              <article
                key={alert.id}
                className="log-row grid grid-cols-[52px_1fr] gap-x-3 gap-y-1 px-4 py-3.5 sm:grid-cols-[64px_1fr_auto] sm:px-5"
              >
                <div className="font-mono text-[10px] uppercase leading-relaxed tracking-[0.06em] text-ink-faint">
                  <span className="block text-ink-soft">{formatTime(alert.created_at)}</span>
                  <span className="block">{timeAgo(alert.created_at)}</span>
                </div>

                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={severityTag[alert.severity] || severityTag.info}>
                      {severityLabel[alert.severity] || 'Info'}
                    </span>
                    <h3 className="text-sm font-semibold leading-snug text-ink">{alert.title}</h3>
                    {acknowledged && <span className="tag text-ink-faint">Acknowledged</span>}
                  </div>
                  <p className="mt-1 text-[13px] leading-relaxed text-ink-soft">{alert.message}</p>
                  <p className="mt-1.5 font-mono text-[10px] uppercase tracking-[0.08em] text-ink-faint">
                    {alert.channel ? `CH ${String(alert.channel).padStart(2, '0')}` : 'All cameras'}
                    {alert.type ? ` · ${alert.type.replace(/_/g, ' ')}` : ''}
                  </p>
                </div>

                {onAcknowledge && !acknowledged && (
                  <button
                    type="button"
                    onClick={() => onAcknowledge(alert.id)}
                    className={cx(
                      'col-start-2 mt-1 justify-self-start border border-rule px-2 py-1 sm:col-start-3 sm:mt-0 sm:self-start',
                      'font-mono text-[10px] uppercase tracking-[0.1em] text-ink-soft transition-colors hover:border-ink hover:text-ink',
                    )}
                  >
                    Acknowledge
                  </button>
                )}
              </article>
            )
          })}
      </div>
    </section>
  )
}
