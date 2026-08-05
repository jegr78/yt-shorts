import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

/** A last-resort boundary so a render-time throw (e.g. an unexpected error deep
 * in a screen) shows a readable message the operator can recover from, instead
 * of React unmounting the whole tree to a blank page. The router's own decoding
 * is already hardened (see scopedApi.safeDecode); this catches everything else. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: '2rem', fontFamily: 'sans-serif' }}>
          <h1>Something went wrong.</h1>
          <p>The studio hit an unexpected error. Reload the page to continue.</p>
          <button type="button" onClick={() => this.setState({ error: null })}>
            Try again
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
