import { Component, type ErrorInfo, type ReactNode } from 'react'
import { invoke } from '@tauri-apps/api/core'

function report(kind: string, message: string, stack?: string) {
  const line = `[crash] ${kind}: ${message}${stack ? ` :: ${stack.split('\n').slice(0, 4).join(' | ')}` : ''}`
  invoke('ui_log', { line }).catch(() => {})
}

/**
 * Errors thrown outside React's render phase (a useFrame callback, a timer, a promise) never
 * reach an error boundary, so they used to vanish into a silent black window. Forward them to
 * the terminal running `npm run tauri dev` instead.
 */
export function installGlobalErrorLogging() {
  window.addEventListener('error', (e) => report('error', e.message, e.error?.stack))
  window.addEventListener('unhandledrejection', (e) => {
    const r = e.reason
    report('unhandled rejection', String(r?.message ?? r), r?.stack)
  })
}

/**
 * Without this, one thrown render error unmounts the whole tree and the window goes pure black
 * (html/body are #000) with nothing to go on. Show what broke, and say it in the terminal too.
 */
export default class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    report('ui crashed', error.message, `${error.stack ?? ''}\n${info.componentStack ?? ''}`)
    console.error('Boogeyman UI crashed:', error, info)
  }

  render() {
    const e = this.state.error
    if (!e) return this.props.children
    return (
      <div className="crash">
        <b>UI CRASHED</b>
        <pre>{e.message}</pre>
        <pre className="crash-stack">{(e.stack ?? '').split('\n').slice(1, 7).join('\n')}</pre>
        <button type="button" onClick={() => this.setState({ error: null })}>
          RETRY
        </button>
      </div>
    )
  }
}
