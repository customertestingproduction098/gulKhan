import { useCallback, useEffect, useState } from 'react'
import { toast } from 'react-toastify'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { IconAlert, IconCheck, IconDownload } from '../components/Icons'
import { interactionApi, reportApi } from '../services/api'
import { cx, formatDate, formatTime, percent } from '../utils/helpers'

const RANGES = [
  { label: '7 days', value: 7 },
  { label: '30 days', value: 30 },
  { label: '90 days', value: 90 },
]

const TABS = [
  { value: 'overview', label: 'Overview' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'monthly', label: 'Monthly' },
]

const tooltipStyle = {
  background: '#0b1220',
  border: '1px solid rgba(255,255,255,0.1)',
  borderRadius: '12px',
  color: '#e2e8f0',
  fontSize: '12px',
}

const today = () => {
  const now = new Date()
  const offset = now.getTimezoneOffset() * 60000
  return new Date(now.getTime() - offset).toISOString().slice(0, 10)
}

function Metric({ label, value, hint, tone }) {
  return (
    <div className="card card-pad">
      <p className="label">{label}</p>
      <p className={cx('value', tone)}>{value}</p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  )
}

function DailyBars({ data = [], loading }) {
  if (!data.length) {
    return (
      <div className="grid h-72 place-items-center text-sm text-slate-500">
        {loading ? 'Loading…' : 'No data for this period'}
      </div>
    )
  }
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.12)" />
        <XAxis
          dataKey="date"
          tick={{ fill: '#64748b', fontSize: 11 }}
          tickFormatter={(value) => value?.slice(5)}
          axisLine={false}
          tickLine={false}
        />
        <YAxis tick={{ fill: '#64748b', fontSize: 11 }} axisLine={false} tickLine={false} allowDecimals={false} />
        <Tooltip contentStyle={tooltipStyle} />
        <Legend wrapperStyle={{ fontSize: 12, color: '#94a3b8' }} />
        <Bar dataKey="successful_greetings" name="Greeted" fill="#10b981" radius={[4, 4, 0, 0]} />
        <Bar dataKey="missed_greetings" name="Missed" fill="#f59e0b" radius={[4, 4, 0, 0]} />
        <Bar dataKey="warehouse_entries" name="Entries" fill="#38bdf8" radius={[4, 4, 0, 0]} />
        <Bar dataKey="alerts" name="Alerts" fill="#f43f5e" radius={[4, 4, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  )
}

export default function Reports() {
  const [tab, setTab] = useState('overview')
  const [days, setDays] = useState(7)
  const [dateValue, setDateValue] = useState(today())
  const [weekValue, setWeekValue] = useState('')
  const [monthValue, setMonthValue] = useState(today().slice(0, 7))
  const [summary, setSummary] = useState(null)
  const [period, setPeriod] = useState(null)
  const [interactions, setInteractions] = useState([])
  const [loading, setLoading] = useState(true)
  const [sending, setSending] = useState(false)

  const loadOverview = useCallback(async (range) => {
    setLoading(true)
    try {
      const [report, log] = await Promise.all([
        reportApi.summary({ days: range }),
        interactionApi.list({ limit: 100, days: range }),
      ])
      setSummary(report)
      setInteractions(log.interactions || [])
    } catch (error) {
      toast.error(error.friendlyMessage || 'Could not load reports')
    } finally {
      setLoading(false)
    }
  }, [])

  const loadPeriod = useCallback(async () => {
    setLoading(true)
    try {
      if (tab === 'daily') setPeriod(await reportApi.daily({ date: dateValue }))
      if (tab === 'weekly') setPeriod(await reportApi.weekly(weekValue ? { week: weekValue } : {}))
      if (tab === 'monthly') setPeriod(await reportApi.monthly(monthValue ? { month: monthValue } : {}))
    } catch (error) {
      toast.error(error.friendlyMessage || 'Could not load report')
    } finally {
      setLoading(false)
    }
  }, [dateValue, monthValue, tab, weekValue])

  useEffect(() => {
    if (tab === 'overview') loadOverview(days)
  }, [days, loadOverview, tab])

  useEffect(() => {
    if (tab !== 'overview') loadPeriod()
  }, [loadPeriod, tab])

  const shop = summary?.shop || {}
  const warehouse = summary?.warehouse || {}
  const daily = summary?.daily || []
  const greetingSplit = [
    { name: 'Greeted', value: shop.greeted ?? shop.successful_greetings ?? 0, color: '#10b981' },
    { name: 'Missed', value: shop.missed ?? shop.missed_greetings ?? 0, color: '#f59e0b' },
  ]
  const trend = tab === 'overview' ? daily : period?.daily || []
  const totals = period?.totals || {}

  const handleSend = async () => {
    setSending(true)
    try {
      const kind = tab === 'weekly' ? 'weekly' : 'daily'
      const value = tab === 'weekly' ? weekValue || undefined : dateValue
      const result = await reportApi.send({ kind, value })
      toast.success(result?.message || 'Report queued for delivery')
    } catch (error) {
      toast.error(error.friendlyMessage || 'Could not send report')
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="label">Analytics</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight text-white sm:text-3xl">Reports</h1>
          <p className="mt-1 text-sm text-slate-400">Daily, weekly, and monthly performance reporting.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <a href={reportApi.pdfUrl({ days })} className="btn-secondary" target="_blank" rel="noreferrer">
            <IconDownload width="16" height="16" /> PDF
          </a>
          <a href={reportApi.csvUrl({ days })} className="btn-secondary" target="_blank" rel="noreferrer">
            <IconDownload width="16" height="16" /> CSV
          </a>
          <button type="button" className="btn-primary" onClick={handleSend} disabled={sending}>
            {sending ? 'Sending…' : 'Email report'}
          </button>
        </div>
      </header>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex rounded-xl border border-white/10 bg-white/5 p-1">
          {TABS.map((item) => (
            <button
              key={item.value}
              type="button"
              onClick={() => setTab(item.value)}
              className={
                tab === item.value
                  ? 'rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-semibold text-emerald-950'
                  : 'rounded-lg px-3 py-1.5 text-xs font-medium text-slate-400 hover:text-slate-100'
              }
            >
              {item.label}
            </button>
          ))}
        </div>

        {tab === 'overview' && (
          <div className="flex rounded-xl border border-white/10 bg-white/5 p-1">
            {RANGES.map((range) => (
              <button
                key={range.value}
                type="button"
                onClick={() => setDays(range.value)}
                className={
                  days === range.value
                    ? 'rounded-lg bg-white/10 px-3 py-1.5 text-xs font-semibold text-white'
                    : 'rounded-lg px-3 py-1.5 text-xs font-medium text-slate-400 hover:text-slate-100'
                }
              >
                {range.label}
              </button>
            ))}
          </div>
        )}
        {tab === 'daily' && <input type="date" className="input max-w-[180px]" value={dateValue} onChange={(e) => setDateValue(e.target.value)} />}
        {tab === 'weekly' && <input className="input max-w-[200px]" placeholder="2026-W42 (optional)" value={weekValue} onChange={(e) => setWeekValue(e.target.value)} />}
        {tab === 'monthly' && <input type="month" className="input max-w-[180px]" value={monthValue} onChange={(e) => setMonthValue(e.target.value)} />}
      </div>

      {tab === 'overview' && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Metric label="Interactions" value={shop.total_interactions ?? 0} hint={`last ${days} days`} />
            <Metric label="Greeting rate" value={`${percent(shop.greeted, shop.total_interactions)}%`} hint={`${shop.missed ?? 0} missed`} tone="text-emerald-300" />
            <Metric label="Warehouse entries" value={warehouse.total_entries ?? 0} hint={`${warehouse.unauthorized_entries ?? 0} unauthorized`} />
            <Metric label="Alerts" value={summary?.alerts?.total ?? 0} hint={`${summary?.alerts?.unacknowledged ?? 0} unacknowledged`} tone="text-rose-300" />
          </div>

          <div className="grid gap-6 xl:grid-cols-[1.4fr_1fr]">
            <section className="card card-pad">
              <h2 className="text-sm font-semibold text-white">Daily activity</h2>
              <p className="text-xs text-slate-500">Interactions, entries and alerts per day</p>
              <div className="mt-6 h-72">
                <DailyBars data={daily} loading={loading} />
              </div>
            </section>

            <section className="card card-pad">
              <h2 className="text-sm font-semibold text-white">Greeting split</h2>
              <p className="text-xs text-slate-500">Shopkeeper responsiveness</p>
              <div className="mt-2 h-56">
                {greetingSplit.every((item) => !item.value) ? (
                  <div className="grid h-full place-items-center text-sm text-slate-500">No interactions yet</div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie data={greetingSplit} dataKey="value" nameKey="name" innerRadius={54} outerRadius={82} paddingAngle={3} stroke="none">
                        {greetingSplit.map((entry) => (
                          <Cell key={entry.name} fill={entry.color} />
                        ))}
                      </Pie>
                      <Tooltip contentStyle={tooltipStyle} />
                    </PieChart>
                  </ResponsiveContainer>
                )}
              </div>
              <div className="mt-2 space-y-2">
                <div className="flex items-center justify-between text-sm">
                  <span className="flex items-center gap-2 text-slate-400">
                    <IconCheck width="14" height="14" className="text-emerald-400" /> Greeted
                  </span>
                  <span className="font-medium text-slate-200">{shop.greeted ?? 0}</span>
                </div>
                <div className="flex items-center justify-between text-sm">
                  <span className="flex items-center gap-2 text-slate-400">
                    <IconAlert width="14" height="14" className="text-amber-400" /> Missed
                  </span>
                  <span className="font-medium text-slate-200">{shop.missed ?? 0}</span>
                </div>
                <div className="flex items-center justify-between border-t border-white/10 pt-2 text-sm">
                  <span className="text-slate-400">Compliance</span>
                  <span className="font-semibold text-emerald-300">{percent(shop.greeted, shop.total_interactions)}%</span>
                </div>
              </div>
            </section>
          </div>

          <section className="card">
            <div className="flex items-center justify-between border-b border-white/10 px-5 py-4">
              <div>
                <h2 className="text-sm font-semibold text-white">Interaction detail</h2>
                <p className="text-xs text-slate-500">Recent shop floor audit trail</p>
              </div>
              <span className="pill bg-white/5 text-slate-400">{interactions.length} rows</span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[620px] text-left text-sm">
                <thead>
                  <tr className="border-b border-white/5 text-xs uppercase tracking-wider text-slate-500">
                    <th className="px-5 py-3 font-medium">Date</th>
                    <th className="px-5 py-3 font-medium">Time</th>
                    <th className="px-5 py-3 font-medium">Type</th>
                    <th className="px-5 py-3 font-medium">Customers</th>
                    <th className="px-5 py-3 font-medium">Response</th>
                    <th className="px-5 py-3 font-medium">Channel</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/5">
                  {loading && (
                    <tr>
                      <td colSpan={6} className="px-5 py-10 text-center text-slate-500">Loading report…</td>
                    </tr>
                  )}
                  {!loading && interactions.length === 0 && (
                    <tr>
                      <td colSpan={6} className="px-5 py-10 text-center text-slate-500">No interactions in this period</td>
                    </tr>
                  )}
                  {interactions.map((item) => (
                    <tr key={item.id} className="text-slate-300 hover:bg-white/[0.02]">
                      <td className="px-5 py-3">{formatDate(item.timestamp || item.created_at)}</td>
                      <td className="px-5 py-3">{formatTime(item.timestamp || item.created_at)}</td>
                      <td className="px-5 py-3">
                        <span
                          className={cx(
                            'pill',
                            item.interaction_type === 'SUCCESSFUL' && 'bg-emerald-500/15 text-emerald-300',
                            item.interaction_type === 'MISSED' && 'bg-amber-500/15 text-amber-300',
                            item.interaction_type === 'UNATTENDED' && 'bg-rose-500/15 text-rose-300',
                          )}
                        >
                          {item.interaction_type || (item.greeting_detected ? 'SUCCESSFUL' : 'MISSED')}
                        </span>
                      </td>
                      <td className="px-5 py-3">{item.customer_count ?? '—'}</td>
                      <td className="px-5 py-3">{item.response_time != null ? `${Math.round(item.response_time)}s` : '—'}</td>
                      <td className="px-5 py-3">{item.channel ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}

      {tab === 'daily' && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Metric label="Customers" value={period?.total_customers ?? 0} hint={period?.date} />
            <Metric label="Interactions" value={period?.total_interactions ?? 0} hint={`${period?.missed_greetings ?? 0} missed`} />
            <Metric label="Unattended" value={period?.unattended_customers ?? 0} hint={`${percent(period?.greeting_rate, 1)}% greeting rate`} tone="text-rose-300" />
            <Metric label="Avg response" value={`${period?.avg_response_time ?? 0}s`} hint={`peak ${period?.peak_hour != null ? `${period.peak_hour}:00` : '—'}`} />
          </div>
          <div className="grid gap-6 xl:grid-cols-[1.4fr_1fr]">
            <section className="card card-pad">
              <h2 className="text-sm font-semibold text-white">Missed interactions</h2>
              <p className="text-xs text-slate-500">Top missed greetings for {period?.date || dateValue}</p>
              <div className="mt-4 divide-y divide-white/5">
                {(period?.top_missed || []).length === 0 && (
                  <p className="py-8 text-center text-sm text-slate-500">No missed interactions.</p>
                )}
                {(period?.top_missed || []).map((item) => (
                  <div key={item.id} className="flex items-center justify-between py-3 text-sm">
                    <div>
                      <p className="text-slate-200">{formatTime(item.timestamp || item.created_at)}</p>
                      <p className="text-xs text-slate-500">Channel {item.channel ?? '—'} · {item.customer_count ?? 0} customers</p>
                    </div>
                    <span className="pill bg-amber-500/15 text-amber-300">missed</span>
                  </div>
                ))}
              </div>
            </section>
            <section className="card card-pad">
              <h2 className="text-sm font-semibold text-white">Warehouse</h2>
              <p className="text-xs text-slate-500">Entry activity for the day</p>
              <dl className="mt-5 space-y-3 text-sm">
                <div className="flex justify-between"><dt className="text-slate-400">Entries</dt><dd className="text-slate-100">{period?.warehouse_entries ?? 0}</dd></div>
                <div className="flex justify-between"><dt className="text-slate-400">After hours</dt><dd className="text-slate-100">{period?.after_hours_entries ?? 0}</dd></div>
                <div className="flex justify-between"><dt className="text-slate-400">Unauthorized</dt><dd className="text-rose-300">{period?.unauthorized_entries ?? 0}</dd></div>
                <div className="flex justify-between"><dt className="text-slate-400">False alarms</dt><dd className="text-slate-100">{period?.false_alarm_entries ?? 0}</dd></div>
                <div className="flex justify-between"><dt className="text-slate-400">Alerts</dt><dd className="text-slate-100">{period?.alerts ?? 0}</dd></div>
                <div className="flex justify-between"><dt className="text-slate-400">Unacknowledged</dt><dd className="text-amber-300">{period?.unacknowledged_alerts ?? 0}</dd></div>
              </dl>
            </section>
          </div>
        </>
      )}

      {['weekly', 'monthly'].includes(tab) && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Metric label="Customers" value={totals.total_customers ?? 0} hint={`${period?.start_date || ''} → ${period?.end_date || ''}`} />
            <Metric label="Greeting rate" value={`${percent(totals.successful_greetings, totals.total_interactions)}%`} hint={`${totals.missed_greetings ?? 0} missed`} tone="text-emerald-300" />
            <Metric label="Warehouse entries" value={totals.warehouse_entries ?? 0} hint={`${totals.after_hours_entries ?? 0} after hours`} />
            <Metric label="False alarm rate" value={`${percent(totals.false_alarms, totals.warehouse_entries)}%`} hint={`${totals.false_alarms ?? 0} false alarms`} tone="text-amber-300" />
          </div>
          <section className="card card-pad">
            <h2 className="text-sm font-semibold text-white capitalize">{tab} trend</h2>
            <p className="text-xs text-slate-500">{period?.week || period?.month || `${period?.start_date || ''} → ${period?.end_date || ''}`}</p>
            <div className="mt-6 h-80">
              <DailyBars data={trend} loading={loading} />
            </div>
          </section>
          <section className="card card-pad">
            <div className="mb-4 flex items-center justify-between">
              <div>
                <h2 className="text-sm font-semibold text-white">Daily breakdown</h2>
                <p className="text-xs text-slate-500">Per-day totals in this period</p>
              </div>
              <span className="pill bg-white/5 text-slate-400">{trend.length} days</span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] text-left text-sm">
                <thead>
                  <tr className="border-b border-white/5 text-xs uppercase tracking-wider text-slate-500">
                    <th className="px-3 py-3 font-medium">Date</th>
                    <th className="px-3 py-3 font-medium">Customers</th>
                    <th className="px-3 py-3 font-medium">Greeted</th>
                    <th className="px-3 py-3 font-medium">Missed</th>
                    <th className="px-3 py-3 font-medium">Entries</th>
                    <th className="px-3 py-3 font-medium">Alerts</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/5">
                  {trend.length === 0 && (
                    <tr>
                      <td colSpan={6} className="px-3 py-8 text-center text-slate-500">{loading ? 'Loading…' : 'No data for this period'}</td>
                    </tr>
                  )}
                  {trend.map((item) => (
                    <tr key={item.date} className="text-slate-300">
                      <td className="px-3 py-3">{item.date}</td>
                      <td className="px-3 py-3">{item.total_customers ?? 0}</td>
                      <td className="px-3 py-3 text-emerald-300">{item.successful_greetings ?? 0}</td>
                      <td className="px-3 py-3 text-amber-300">{item.missed_greetings ?? 0}</td>
                      <td className="px-3 py-3">{item.warehouse_entries ?? 0}</td>
                      <td className="px-3 py-3">{item.alerts ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </div>
  )
}
