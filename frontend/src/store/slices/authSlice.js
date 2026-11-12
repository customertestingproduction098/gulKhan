import { createSlice } from '@reduxjs/toolkit'

const STORAGE_KEY = 'surveillance.owner'

const readStoredOwner = () => {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

const persist = (owner) => {
  try {
    if (owner) localStorage.setItem(STORAGE_KEY, JSON.stringify(owner))
    else localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* storage may be unavailable (private mode) */
  }
}

const storedOwner = readStoredOwner()

const initialState = {
  owner: storedOwner,
  isAuthenticated: Boolean(storedOwner),
}

const authSlice = createSlice({
  name: 'auth',
  initialState,
  reducers: {
    signIn(state, action) {
      const owner = {
        name: action.payload.name?.trim() || 'Owner',
        email: action.payload.email?.trim() || '',
        role: action.payload.role || 'owner',
        signedInAt: new Date().toISOString(),
      }
      state.owner = owner
      state.isAuthenticated = true
      persist(owner)
    },
    signOut(state) {
      state.owner = null
      state.isAuthenticated = false
      persist(null)
    },
  },
})

export const { signIn, signOut } = authSlice.actions
export default authSlice.reducer
