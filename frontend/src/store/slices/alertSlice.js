import { createAsyncThunk, createSlice } from '@reduxjs/toolkit'
import { alertApi } from '../../services/api'

const initialState = {
  items: [],
  total: 0,
  loading: false,
  error: null,
  lastFetchedAt: null,
}

export const fetchAlerts = createAsyncThunk(
  'alerts/fetch',
  async (params = {}, { rejectWithValue }) => {
    try {
      return await alertApi.list(params)
    } catch (error) {
      return rejectWithValue(error.friendlyMessage)
    }
  },
)

export const acknowledgeAlert = createAsyncThunk(
  'alerts/acknowledge',
  async (id, { rejectWithValue }) => {
    try {
      return await alertApi.acknowledge(id)
    } catch (error) {
      return rejectWithValue(error.friendlyMessage)
    }
  },
)


const alertSlice = createSlice({
  name: 'alerts',
  initialState,
  reducers: {
    upsertAlert(state, action) {
      const incoming = action.payload
      if (!incoming?.id) return
      const index = state.items.findIndex((item) => item.id === incoming.id)
      if (index >= 0) {
        state.items[index] = { ...state.items[index], ...incoming }
      } else {
        state.items.unshift(incoming)
        state.total = Math.max(state.total + 1, state.items.length)
      }
    },
    markAllRead(state) {
      state.items = state.items.map((alert) => ({ ...alert, acknowledged: true }))
    },
    clearAlerts(state) {
      state.items = []
      state.total = 0
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(fetchAlerts.pending, (state) => {
        state.loading = true
        state.error = null
      })
      .addCase(fetchAlerts.fulfilled, (state, action) => {
        state.loading = false
        const alerts = action.payload?.alerts ?? action.payload ?? []
        state.items = alerts
        state.total = action.payload?.total ?? alerts.length
        state.lastFetchedAt = new Date().toISOString()
      })
      .addCase(fetchAlerts.rejected, (state, action) => {
        state.loading = false
        state.error = action.payload || 'Unable to load alerts'
      })
      .addCase(acknowledgeAlert.fulfilled, (state, action) => {
        const id = action.payload?.id ?? action.meta.arg
        state.items = state.items.map((alert) =>
          alert.id === id
            ? {
                ...alert,
                ...(action.payload?.alert || {}),
                acknowledged: true,
                status: 'ACKNOWLEDGED',
                acknowledged_at: action.payload?.acknowledged_at || alert.acknowledged_at,
              }
            : alert,
        )
      })
      .addCase(acknowledgeAlert.rejected, (state, action) => {
        state.error = action.payload || 'Unable to acknowledge alert'
      })
  },
})

export const { upsertAlert, markAllRead, clearAlerts } = alertSlice.actions
export default alertSlice.reducer
