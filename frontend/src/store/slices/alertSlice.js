import { createAsyncThunk, createSlice } from '@reduxjs/toolkit'
import { alertApi } from '../../services/api'

const initialState = {
  items: [],
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
    markAllRead(state) {
      state.items = state.items.map((alert) => ({ ...alert, acknowledged: true }))
    },
    clearAlerts(state) {
      state.items = []
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
        state.items = action.payload?.alerts ?? action.payload ?? []
        state.lastFetchedAt = new Date().toISOString()
      })
      .addCase(fetchAlerts.rejected, (state, action) => {
        state.loading = false
        state.error = action.payload || 'Unable to load alerts'
      })
      .addCase(acknowledgeAlert.fulfilled, (state, action) => {
        const id = action.payload?.id ?? action.meta.arg
        state.items = state.items.map((alert) =>
          alert.id === id ? { ...alert, acknowledged: true } : alert,
        )
      })
      .addCase(acknowledgeAlert.rejected, (state, action) => {
        state.error = action.payload || 'Unable to acknowledge alert'
      })
  },
})

export const { markAllRead, clearAlerts } = alertSlice.actions
export default alertSlice.reducer
