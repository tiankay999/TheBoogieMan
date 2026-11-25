import { useEffect, useState } from 'react'
import { hud } from '../hud/store'
import { IS_LINUX } from '../three/VideoBackground'

/** Says so on screen when a background video can't play on this machine. */
export default function BgNotice() {
  const [missing, setMissing] = useState<string | null>(null)
  useEffect(() => {
    const id = window.setInterval(() => setMissing(hud.bgMissing), 500)
    return () => window.clearInterval(id)
  }, [])
  useEffect(() => {
    if (!missing) return
    const id = window.setTimeout(() => (hud.bgMissing = null), 15000)
    return () => window.clearTimeout(id)
  }, [missing])
  if (!missing) return null
  return (
    <div className="bg-notice">
      <b>{missing.toUpperCase()} VIDEO WON'T PLAY HERE</b>
      <span>
        {IS_LINUX ? (
          <>
            Run <code>sudo apt install gstreamer1.0-plugins-good gstreamer1.0-libav</code> and restart. Details are in the terminal (lines starting with{' '}
            <code>[bg]</code>).
          </>
        ) : (
          <>
            This machine&apos;s webview can&apos;t decode that clip. Details are in the terminal (lines starting with <code>[bg]</code>).
          </>
        )}
      </span>
    </div>
  )
}
