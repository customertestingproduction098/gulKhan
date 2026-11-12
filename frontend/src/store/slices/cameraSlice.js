import { createAsyncThunk, createSlice } from '@reduxjs/toolkit'
import { cameraApi, healthApi } from '../../services/api'
import { MONITOR_MODES } from '../../utils/helpers'

const DEFAULT_ALLOWED = [2, 4, 5, 7, 8, 9, 11]

const initialState = {
  selectedChannel: 2,
  totalChannels: 7,
  allowedChannels: DEFAULT_ALLOWED,
  mode: MONITOR_MODES.WAREHOUSE,
  status: null,
  health: null,
  loading: false,
  error: null,
}

export const fetchCameraStatus = createAsyncThunk(
  'camera/fetchStatus',
  async (_, { rejectWithValue }) => {
    try {
      return await cameraApi.status()
    } catch (error) {
      return rejectWithValue(error.friendlyMessage)
    }
  },
)

export const checkBackendHealth = createAsyncThunk(
  'camera/health',
  async (_, { rejectWithValue }) => {
    try {
      return await healthApi.check()
    } catch (error) {
      return rejectWithValue(error.friendlyMessage)
    }
  },
)

export const startMonitoring = createAsyncThunk(
  'camera/start',
  async ({ channel, mode }, { rejectWithValue }) => {
    try {
      return await cameraApi.startMonitor({ channel, mode })
    } catch (error) {
      return rejectWithValue(error.friendlyMessage)
    }
  },
)

export const stopMonitoring = createAsyncThunk(
  'camera/stop',
  async (_, { rejectWithValue }) => {
    try {
      return await cameraApi.stopMonitor()
    } catch (error) {
      return rejectWithValue(error.friendlyMessage)
    }
  },
)

const cameraSlice = createSlice({
  name: 'camera',
  initialState,
  reducers: {
    selectChannel(state, action) {
      const ch = Number(action.payload) || DEFAULT_ALLOWED[0]
      state.selectedChannel = ch
    },
    selectMode(state, action) {
      state.mode = action.payload
    },
    clearCameraError(state) {
      state.error = null
    },
  },
  extraReducers: (builder) => {
    const pending = (state) => {
      state.loading = true
      state.error = null
    }
    const rejected = (state, action) => {
      state.loading = false
      state.error = action.payload || 'Request failed'
    }

    builder
      .addCase(fetchCameraStatus.pending, pending)
      .addCase(fetchCameraStatus.fulfilled, (state, action) => {
        state.loading = false
        state.status = action.payload
        if (action.payload?.total_channels) {
          state.totalChannels = action.payload.total_channels
        }
        if (action.payload?.allowed_channels) {
          state.allowedChannels = action.payload.allowed_channels
        }
        if (
          state.allowedChannels?.length &&
          !state.allowedChannels.includes(state.selectedChannel)
        ) {
          state.selectedChannel = state.allowedChannels[0]
        }
      })
      .addCase(fetchCameraStatus.rejected, rejected)
      .addCase(checkBackendHealth.pending, pending)
      .addCase(checkBackendHealth.fulfilled, (state, action) => {
        state.loading = false
        state.health = action.payload
      })
      .addCase(checkBackendHealth.rejected, (state, action) => {
        state.loading = false
        state.health = { status: 'offline', error: action.payload }
      })
      .addCase(startMonitoring.pending, pending)
      .addCase(startMonitoring.fulfilled, (state, action) => {
        state.loading = false
        state.status = { ...state.status, ...action.payload }
      })
      .addCase(startMonitoring.rejected, rejected)
      .addCase(stopMonitoring.pending, pending)
      .addCase(stopMonitoring.fulfilled, (state) => {
        state.loading = false
        if (state.status) state.status.monitoring = false
      })
      .addCase(stopMonitoring.rejected, rejected)
  },
})

export const { selectChannel, selectMode, clearCameraError } = cameraSlice.actions
export default cameraSlice.reducer
