import { useState } from 'react'
import { useDispatch } from 'react-redux'
import { authApi } from '../services/api'
import { signIn } from '../store/slices/authSlice'
import { IconEye, IconEyeOff } from './Icons'

const today = new Date().toLocaleDateString(undefined, {
  weekday: 'short',
  day: '2-digit',
  month: 'short',
  year: 'numeric',
})

export default function SignIn() {
  const dispatch = useDispatch()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async (event) => {
    event.preventDefault()
    if (!email.trim()) {
      setError('Enter your email or username to open the daybook.')
      return
    }
    if (!password) {
      setError('Enter your password to open the daybook.')
      return
    }
    setSubmitting(true)
    setError('')
    try {
      const data = await authApi.login({ email, password })
      dispatch(signIn(data.owner || {}))
    } catch (requestError) {
      setError(requestError.friendlyMessage || 'Invalid email, username, or password.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="grid min-h-screen place-items-center px-4 py-10">
      <div className="w-full max-w-[440px]">
        <div className="mb-3 flex items-baseline justify-between gap-3">
          <span className="eyebrow">Sentinel · Daybook</span>
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
            {today}
          </span>
        </div>

        <form onSubmit={handleSubmit} className="sheet">
          <div className="border-l-2 border-rust px-5 py-6 sm:px-7">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h1 className="font-display text-5xl font-extrabold uppercase leading-[0.86] tracking-[0.01em] text-ink">
                  Sentinel
                </h1>
                <p className="mt-2 font-mono text-[10px] uppercase tracking-[0.2em] text-ink-soft">
                  Shop &amp; warehouse daybook
                </p>
              </div>
              <span className="stamp stamp-quiet" aria-hidden="true">
                Local
              </span>
            </div>

            <p className="mt-6 border-t border-rule pt-4 text-sm leading-relaxed text-ink-soft">
              Sign the book to open today&rsquo;s sheet. Your account heads the log and the
              reports.
            </p>

            <div className="mt-5 space-y-4">
              <label className="block">
                <span className="eyebrow">Sign-in Email or Username</span>
                <input
                  type="text"
                  className="input mt-1.5"
                  value={email}
                  onChange={(event) => {
                    setEmail(event.target.value)
                    setError('')
                  }}
                  autoComplete="username"
                  placeholder="admin123@gmail.com or admin"
                  autoFocus
                  required
                />
              </label>

              <label className="block">
                <span className="eyebrow">Password</span>
                <div className="relative mt-1.5 flex items-center">
                  <input
                    type={showPassword ? 'text' : 'password'}
                    className="input pr-10"
                    value={password}
                    onChange={(event) => {
                      setPassword(event.target.value)
                      setError('')
                    }}
                    autoComplete="current-password"
                    placeholder="••••••••"
                    required
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
              </label>
            </div>

            {error && (
              <p className="mt-4 border-l-2 border-rust pl-3 text-sm text-rust" role="alert">
                {error}
              </p>
            )}

            <button
              type="submit"
              disabled={submitting}
              className="btn-primary mt-6 w-full justify-center"
            >
              {submitting ? 'Verifying…' : 'Open daybook'}
            </button>
          </div>
        </form>
      </div>
    </main>
  )
}
