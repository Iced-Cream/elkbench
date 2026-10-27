export type Waiter = { who: string; purpose: string; since: number }
export type Handoff = {
  time?: number
  holder?: string
  ended?: string
  note?: string
  os?: string
  active_project?: string
}
export type Unit = {
  device: string
  connected?: boolean | null
  holder: string | null
  purpose: string | null
  since: number | null
  used: number | null
  expires: number | null
  running: string[]
  running_since?: (number | null)[]
  waiting: Waiter[]
  handoff: Handoff | null
}
export type Snapshot = { now: number; devices: Unit[]; error?: string }

declare module 'claude-code' {
  interface PluginState {
    'elklock-pane': { snapshot: Snapshot; pending: Record<string, string> }
  }
}
