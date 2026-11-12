import { configureStore } from '@reduxjs/toolkit'
import alertReducer from './slices/alertSlice'
import authReducer from './slices/authSlice'
import cameraReducer from './slices/cameraSlice'

export const store = configureStore({
  reducer: {
    camera: cameraReducer,
    alerts: alertReducer,
    auth: authReducer,
  },
  devTools: import.meta.env.DEV,
})

export default store
