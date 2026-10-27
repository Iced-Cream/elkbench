import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Snapshot, Unit } from '../types'

const PANE = 'elklock'
const TITLE = 'Elektron units'
// The Python that runs elklock.py (userConfig `python`), and elklock.py beside this pane's folder.
let PYTHON = 'python'
const elklock = ($: EngineInterface) => $.plugin.root + '/../elklock.py'
const EVERY_MS = 3000
// The name a claim from the pane carries; `elklock.py ... --manual me` from a shell is the same holder.
const ME = 'me'

const pending = atom({ plugin: 'elklock-pane', key: 'pending' } as const, {} as Record<string, string>)

const snapshot = atom({ plugin: 'elklock-pane', key: 'snapshot' } as const, {
  now: 0,
  devices: [],
} as Snapshot)

const mins = (from: number | null | undefined, to: number) =>
  from ? Math.max(0, Math.floor((to - from) / 60)) : 0
const clock = (t: number | null | undefined) =>
  t ? new Date(t * 1000).toTimeString().slice(0, 5) : '?'
const short = (who: string | null) =>
  !who ? '' : who.startsWith('session ') ? 'session ' + who.slice(8, 16) : who

async function poll($: EngineInterface) {
  let snap: Snapshot
  try {
    const r = await $.process.run([PYTHON, elklock($), 'status', '--json'], { timeoutMs: 15000 })
    snap = r.exitCode === 0
      ? (JSON.parse(r.stdout) as Snapshot)
      : { now: Date.now() / 1000, devices: [], error: r.stderr.trim().slice(0, 300) || 'exit ' + r.exitCode }
  } catch (err) {
    snap = { now: Date.now() / 1000, devices: [], error: String(err).slice(0, 300) }
  }
  await update($, snapshot, () => snap)
  const held = snap.devices.filter(u => u.holder)
  $.ui.status(held.length ? 'elklock: ' + held.map(u => u.device).join(', ') + ' held' : undefined)
}

// Claim or release a unit for the person, then redraw from a fresh status.
async function act($: EngineInterface, device: string, verb: 'claim' | 'release') {
  await update($, pending, p => ({ ...p, [device]: verb === 'claim' ? 'claiming…' : 'releasing…' }))
  const argv = verb === 'claim'
    ? [PYTHON, elklock($), 'claim', device, '--manual', ME, '--for', 'manual use (desktop pane)', '--max', '4h']
    : [PYTHON, elklock($), 'release', device, '--manual', ME, '--note', 'released from the desktop pane']
  try {
    const r = await $.process.run(argv, { timeoutMs: 90000 })
    const out = (r.stdout + r.stderr).trim().split(String.fromCharCode(10))[0]?.trim() ?? ''
    $.ui.toast(r.exitCode === 0 ? out : `${device}: ${verb} refused: ${out}`)
  } catch (err) {
    $.ui.toast(`${device}: ${verb} failed: ${String(err).slice(0, 200)}`)
  }
  await update($, pending, p => {
    const { [device]: _, ...rest } = p
    return rest
  })
  await poll($)
}

export const register: Register = (on, options) => {
  if (typeof options.python === 'string' && options.python) PYTHON = options.python
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'elklock', description: 'Show who holds each Elektron unit' })
    await poll($)
    $.clock.every(EVERY_MS, () => poll($))
    void $.ui.open({ id: PANE, title: TITLE })
    return next(e)
  })

  on('command.run', { command: 'elklock' }, async $ => {
    await $.ui.open({ id: PANE, title: TITLE })
    return { text: 'elklock pane opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const snap = await read($, snapshot)
    const busy = await read($, pending)
    if (snap.error) return <Text color="red">elklock status failed: {snap.error}</Text>
    if (!snap.devices.length) return <Text dimColor>Reading elklock…</Text>
    const now = snap.now
    const control = (u: Unit) => {
      if (busy[u.device]) return <Text dimColor>{busy[u.device]}</Text>
      if (!u.holder)
        return <Button key={'claim-' + u.device} label="Claim" onPress={() => void act($, u.device, 'claim')} />
      if (u.holder === 'person: ' + ME)
        return (
          <Button key={'release-' + u.device} label="Release" variant="primary"
            onPress={() => void act($, u.device, 'release')} />
        )
      return null
    }
    const unit = (u: Unit) => (
      <Box flexDirection="column" marginBottom={1}>
        <Box flexDirection="row" justifyContent="space-between" alignItems="center" gap={1}>
          <Text bold>{u.device}</Text>
          {control(u)}
        </Box>
        <Text>
          {u.holder ? (
            <Text>
              <Text color="yellow">held</Text> by {short(u.holder)} since {clock(u.since)}, idle{' '}
              {mins(u.used, now)} min, {mins(now, u.expires ?? now)} min max left
            </Text>
          ) : (
            <Text color="green">free</Text>
          )}
          {' · '}
          {u.connected === false ? (
            <Text color="red">not connected</Text>
          ) : u.connected ? (
            <Text color="green">connected</Text>
          ) : (
            <Text dimColor>connection unknown</Text>
          )}
        </Text>
        {u.purpose && <Text dimColor>  for: {u.purpose}</Text>}
        {u.running.map((c, i) => (
          <Text color="cyan">
            {'  '}running {Math.max(0, Math.round(now - (u.running_since?.[i] ?? now)))} s: {c.slice(0, 120)}
          </Text>
        ))}
        {u.waiting.map(w => (
          <Text dimColor>  waiting: {short(w.who)} since {clock(w.since)}, for {w.purpose}</Text>
        ))}
        {u.handoff && (
          <Text dimColor>
            {'  '}last handoff {clock(u.handoff.time)} ({u.handoff.ended}){u.handoff.os ? ', ' + u.handoff.os.replace(/\s+/g, ' ') : ''}
            {u.handoff.active_project ? ', active: ' + u.handoff.active_project : ''}
            {u.handoff.note ? ': ' + u.handoff.note : ''}
          </Text>
        )}
      </Box>
    )
    return (
      <Box flexDirection="column">
        {snap.devices.map(unit)}
        <Text dimColor>updated {clock(now)}, every {EVERY_MS / 1000} s</Text>
      </Box>
    )
  })
}
