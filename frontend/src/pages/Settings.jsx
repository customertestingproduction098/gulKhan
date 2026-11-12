import { useEffect, useState } from 'react'
import { toast } from 'react-toastify'
import { IconEye, IconEyeOff, IconRefresh, IconSettings } from '../components/Icons'
import { settingsApi } from '../services/api'
import { cx } from '../utils/helpers'

function Field({ label, hint, children, wide }) {
  return (
    <label className={cx('block', wide && 'sm:col-span-2')}>
      <span className="eyebrow">{label}</span>
      <span className="mt-1.5 block">{children}</span>
      {hint && (
        <span className="mt-1 block font-mono text-[10px] leading-relaxed tracking-[0.03em] text-ink-faint">
          {hint}
        </span>
      )}
    </label>
  )
}

function Section({ title, description, children }) {
  return (
    <section className="sheet">
      <div className="border-b border-rule px-4 py-3 sm:px-5">
        <h2 className="text-sm font-semibold text-ink">{title}</h2>
        {description && <p className="note mt-0.5">{description}</p>}
      </div>
      <div className="p-4 sm:p-5">{children}</div>
    </section>
  )
}

export default function Settings() {
  const [settings, setSettings] = useState(null)
  const [features, setFeatures] = useState({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [showPassword, setShowPassword] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const data = await settingsApi.get()
      setSettings(data.settings || data)
      setFeatures(data.features || {})
    } catch (error) {
      toast.error(error.friendlyMessage || 'Could not load settings')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const updateOwner = (field, value) => {
    setSettings((current) => ({
      ...current,
      owner: {
        ...(current?.owner || {}),
        [field]: value,
      },
    }))
  }

  const save = async () => {
    setSaving(true)
    try {
      const payload = { ...settings }
      const data = await settingsApi.update(payload)
      setSettings(data.settings || data)
      setFeatures(data.features || features)
      toast.success('Settings saved successfully')
    } catch (error) {
      toast.error(error.friendlyMessage || 'Could not save settings')
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return <div className="sheet sheet-pad text-sm text-ink-soft">Reading settings…</div>
  }

  if (!settings) {
    return (
      <div className="sheet sheet-pad">
        <p className="text-sm text-ink-soft">Settings are unavailable right now.</p>
        <button type="button" onClick={load} className="btn-secondary mt-4">
          <IconRefresh width="14" height="14" /> Try again
        </button>
      </div>
    )
  }

  const owner = settings.owner || {}

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="mt-2 font-display text-4xl font-extrabold uppercase leading-none tracking-[0.01em] text-ink sm:text-5xl">
            Settings
          </h1>
        </div>
        <div className="flex gap-2">
          <button type="button" onClick={load} className="btn-secondary" disabled={saving}>
            <IconRefresh width="14" height="14" /> Reset
          </button>
          <button type="button" onClick={save} className="btn-primary" disabled={saving}>
            <IconSettings width="14" height="14" /> {saving ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      </header>

      <div className="grid gap-6">
        {/* SECTION 1: Authentication / Sign-in Credentials */}
        <Section
          title="Dashboard Authentication"
          description="Credentials used strictly for logging into this surveillance dashboard."
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="Sign-in Email (Auth)"
              hint="Email address used strictly for logging into the dashboard."
            >
              <input
                type="email"
                className="input"
                value={owner.auth_email ?? ''}
                onChange={(e) => updateOwner('auth_email', e.target.value)}
                autoComplete="email"
                required
              />
            </Field>

            <Field
              label="Sign-in Username"
              hint="Alternative username for dashboard sign-in."
            >
              <input
                type="text"
                className="input"
                value={owner.username ?? ''}
                onChange={(e) => {
                  updateOwner('username', e.target.value)
                  updateOwner('name', e.target.value)
                }}
                autoComplete="username"
                required
              />
            </Field>

            <Field
              label="Sign-in Password"
              hint="Password for dashboard sign-in. Click the eye icon to view or hide."
              wide
            >
              <div className="relative flex items-center">
                <input
                  type={showPassword ? 'text' : 'password'}
                  className="input pr-10"
                  value={owner.password ?? ''}
                  onChange={(e) => updateOwner('password', e.target.value)}
                  autoComplete="current-password"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((prev) => !prev)}
                  className="absolute right-2.5 p-1 text-ink-faint transition-colors hover:text-ink"
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  tabIndex="-1"
                >
                  {showPassword ? (
                    <IconEyeOff width="16" height="16" />
                  ) : (
                    <IconEye width="16" height="16" />
                  )}
                </button>
              </div>
            </Field>
          </div>
        </Section>

        {/* SECTION 2: Logs, Alerts & Updates Delivery */}
        <Section
          title="Logs & Updates Delivery"
          description="Where real-time camera alerts, intrusion logs, snapshots, and warehouse updates are delivered."
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="Notification Email (Logs & Updates)"
              hint="The primary email address where the client will receive all surveillance alerts, unauthorized intrusion notifications, camera outcome logs, and Cloudinary snapshot pictures."
              wide
            >
              <input
                type="email"
                className="input"
                value={owner.notification_email ?? ''}
                onChange={(e) => updateOwner('notification_email', e.target.value)}
                required
              />
            </Field>

            <Field
              label="Secondary Notification Email (Optional)"
              hint="Optional secondary/backup email to receive copies of critical alerts."
              wide
            >
              <input
                type="email"
                className="input"
                value={owner.secondary_email ?? ''}
                onChange={(e) => updateOwner('secondary_email', e.target.value)}
              />
            </Field>
          </div>
        </Section>
      </div>

      <div className="flex justify-end border-t border-rule pt-4">
        <button type="button" onClick={save} className="btn-primary" disabled={saving}>
          {saving ? 'Saving…' : 'Save changes'}
        </button>
      </div>
    </div>
  )
}
