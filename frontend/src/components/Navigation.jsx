import { NavLink, useNavigate } from 'react-router-dom'
import { useDispatch, useSelector } from 'react-redux'
import { signOut } from '../store/slices/authSlice'
import { cx } from '../utils/helpers'
import {
  IconBell,
  IconGrid,
  IconLogout,
  IconSettings,
  IconShield,
  IconStore,
  IconWarehouse,
} from './Icons'

const links = [
  { to: '/', label: 'Today', icon: IconGrid, end: true },
  { to: '/warehouse', label: 'Warehouse', icon: IconWarehouse },
  { to: '/alerts', label: 'Alerts', icon: IconBell },
  { to: '/settings', label: 'Settings', icon: IconSettings },
]

const today = new Date().toLocaleDateString(undefined, {
  weekday: 'short',
  day: '2-digit',
  month: 'short',
})

function Unread({ count }) {
  if (!count) return null
  return (
    <span className="ml-auto font-mono text-[11px] font-bold text-rust">[{count}]</span>
  )
}

export default function Navigation({ backendOnline, liveConnected = false }) {
  const dispatch = useDispatch()
  const navigate = useNavigate()
  const { owner } = useSelector((state) => state.auth)
  const alerts = useSelector((state) => state.alerts.items)
  const unread = alerts.filter((alert) => !alert.acknowledged).length

  const handleSignOut = () => {
    dispatch(signOut())
    navigate('/')
  }

  return (
    <>
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-60 flex-col border-r border-rule bg-paper lg:flex">
        <div className="border-b border-rule px-5 py-5">
          <div className="flex items-center gap-3">
            <span className="grid h-9 w-9 place-items-center border border-ink bg-ink text-sheet">
              <IconShield width="18" height="18" />
            </span>
            <div className="min-w-0">
              <p className="font-display text-[26px] font-extrabold uppercase leading-none tracking-[0.06em] text-ink">
                Sentinel
              </p>
              <p className="mt-1 font-mono text-[9px] uppercase tracking-[0.22em] text-ink-soft">
                Shop &amp; warehouse daybook
              </p>
            </div>
          </div>
          <p className="mt-5 font-mono text-[10px] uppercase tracking-[0.16em] text-ink-faint">
            {today}
          </p>
        </div>

        <nav className="flex flex-1 flex-col px-2 py-4" aria-label="Main">
          {links.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cx(
                  'group relative flex items-center gap-2.5 py-2.5 pl-3 pr-3 text-sm transition-colors',
                  isActive
                    ? 'font-semibold text-ink'
                    : 'text-ink-soft hover:bg-sheet-deep hover:text-ink',
                )
              }
            >
              {({ isActive }) => (
                <>
                  <span
                    aria-hidden="true"
                    className={cx(
                      'absolute left-0 top-1.5 h-[calc(100%-12px)] w-[2px]',
                      isActive ? 'bg-rust' : 'bg-transparent',
                    )}
                  />
                  <Icon width="16" height="16" className={isActive ? 'text-rust' : 'text-ink-faint'} />
                  <span className="tracking-[0.01em]">{label}</span>
                  {label === 'Alerts' && <Unread count={unread} />}
                </>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-rule px-4 py-4">
          <div className="ledger font-mono text-[10px] uppercase tracking-[0.14em]">
            <span className="text-ink-faint">Link</span>
            <span className="leader" aria-hidden="true" />
            <span className={backendOnline ? 'text-sea' : 'text-rust'}>
              {backendOnline ? 'Online' : 'Offline'}
            </span>
          </div>
          <div className="ledger mt-1.5 font-mono text-[10px] uppercase tracking-[0.14em]">
            <span className="text-ink-faint">Feed</span>
            <span className="leader" aria-hidden="true" />
            <span className={liveConnected ? 'text-sea' : 'text-ink-soft'}>
              {liveConnected ? 'Live' : 'Polling'}
            </span>
          </div>

          <div className="mt-4 flex items-center gap-2.5 border-t border-rule-soft pt-3">
            <span className="grid h-8 w-8 place-items-center border border-rule bg-sheet font-mono text-xs font-bold text-ink">
              {owner?.name?.slice(0, 1)?.toUpperCase() || 'O'}
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-semibold text-ink">{owner?.name}</p>
              <p className="truncate font-mono text-[10px] text-ink-faint">{owner?.email || 'Owner'}</p>
            </div>
            <button
              type="button"
              onClick={handleSignOut}
              title="Sign out"
              className="p-1 text-ink-faint transition-colors hover:text-rust"
            >
              <IconLogout width="15" height="15" />
            </button>
          </div>
        </div>
      </aside>

      <header className="sticky top-0 z-30 border-b border-rule bg-paper lg:hidden">
        <div className="flex items-center gap-3 px-4 py-3">
          <span className="grid h-8 w-8 place-items-center border border-ink bg-ink text-sheet">
            <IconShield width="16" height="16" />
          </span>
          <div>
            <p className="font-display text-xl font-extrabold uppercase leading-none tracking-[0.06em] text-ink">
              Sentinel
            </p>
            <p className="font-mono text-[9px] uppercase tracking-[0.18em] text-ink-soft">{today}</p>
          </div>
          <span
            className={cx(
              'ml-auto border px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase tracking-[0.12em]',
              backendOnline ? 'border-sea text-sea' : 'border-rust text-rust',
            )}
          >
            {backendOnline ? 'Online' : 'Offline'}
          </span>
          <button
            type="button"
            onClick={handleSignOut}
            className="text-ink-faint"
            title="Sign out"
          >
            <IconLogout width="16" height="16" />
          </button>
        </div>
        <nav className="flex gap-1 overflow-x-auto px-2 pb-2" aria-label="Main">
          {links.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cx(
                  'flex shrink-0 items-center gap-1.5 border-b-2 px-2 py-1.5 text-xs transition-colors',
                  isActive
                    ? 'border-rust font-semibold text-ink'
                    : 'border-transparent text-ink-soft',
                )
              }
            >
              <Icon width="13" height="13" />
              {label}
              {label === 'Alerts' && unread > 0 && (
                <span className="font-mono text-[10px] font-bold text-rust">{unread}</span>
              )}
            </NavLink>
          ))}
        </nav>
      </header>
    </>
  )
}
