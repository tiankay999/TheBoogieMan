import { useFrame, useThree } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import * as THREE from 'three'
import { invoke } from '@tauri-apps/api/core'
import { hud } from '../hud/store'

/** Print to the terminal running `make dev` (and the devtools console). */
function bgLog(line: string) {
  console.info('[bg]', line)
  invoke('ui_log', { line: `[bg] ${line}` }).catch(() => {})
}

const STALL_MS = 4000 // a video that hasn't moved for this long counts as stuck

/** Only Linux/WebKitGTK needs the GStreamer codec packages; other webviews decode natively. */
export const IS_LINUX =
  typeof navigator !== 'undefined' && /Linux|X11/.test(navigator.userAgent) && !/Android/.test(navigator.userAgent)
const CODEC_FIX = IS_LINUX ? ' Fix: sudo apt install gstreamer1.0-plugins-good gstreamer1.0-libav, then restart.' : ''

/**
 * Background videos playing behind everything, seen through a blur layer.
 * The blur is cheap and works everywhere: each frame is drawn into a small canvas
 * and stretched back up with smooth filtering. The canvas size sets the blur:
 * tiny = heavy fog (nebula), larger = just softened (the sea, so you can see it).
 * `dim` darkens it so the HUD stays readable.
 *
 * Switch by voice ("change background", "sea background", "background off") or Ctrl+B.
 * Drop your own clip in app/public/bg/ as custom.mp4 or custom.webm and it joins the list.
 */
export type Background = { name: string; sources: string[]; w: number; h: number; dim: number }

export const BACKGROUNDS: Background[] = [
  { name: 'nebula', sources: ['/bg/background.webm'], w: 128, h: 72, dim: 0.1 },
  { name: 'sea', sources: ['/bg/sea.webm', '/bg/sea.mp4'], w: 560, h: 315, dim: 0.48 },
  { name: 'custom', sources: ['/bg/custom.webm', '/bg/custom.mp4'], w: 480, h: 270, dim: 0.45 },
]

export function backgroundByName(name: string) {
  return BACKGROUNDS.find((b) => b.name === name) ?? BACKGROUNDS[0]
}

export default function VideoBackground({ devil }: { devil: React.RefObject<number> }) {
  const { scene } = useThree()
  const fade = useRef(0)
  const current = useRef<string>('')
  const texRef = useRef<THREE.CanvasTexture | null>(null)
  const nextRef = useRef<() => void>(() => {})
  const watch = useRef({ t: -1, at: 0, ok: false })
  const { video, canvas, ctx } = useMemo(() => {
    const video = document.createElement('video')
    video.muted = true
    video.loop = true
    video.playsInline = true
    video.crossOrigin = 'anonymous'
    const canvas = document.createElement('canvas')
    const ctx = canvas.getContext('2d')!
    return { video, canvas, ctx }
  }, [])

  // WebGL textures can't change size once uploaded, so each background size gets a fresh one.
  const makeTexture = () => {
    texRef.current?.dispose()
    const tex = new THREE.CanvasTexture(canvas)
    tex.minFilter = THREE.LinearFilter
    tex.magFilter = THREE.LinearFilter
    tex.generateMipmaps = false
    tex.colorSpace = THREE.NoColorSpace // the scene renders linear-in/linear-out
    texRef.current = tex
    return tex
  }

  // Load the selected background, trying its sources in order; report ones that don't exist.
  const load = (bg: Background) => {
    current.current = bg.name
    if (canvas.width !== bg.w || canvas.height !== bg.h || !texRef.current) {
      canvas.width = bg.w
      canvas.height = bg.h
      if (scene.background === texRef.current) scene.background = null
      makeTexture()
    }
    let i = 0
    const next = () => {
      if (current.current !== bg.name) return
      if (i >= bg.sources.length) {
        hud.bgMissing = bg.name
        const can = ['video/webm; codecs="vp9"', 'video/webm; codecs="vp8"', 'video/mp4; codecs="avc1.42E01E"']
          .map((t) => `${t.split(';')[0]}/${t.split('"')[1]}=${video.canPlayType(t) || 'no'}`)
          .join(' ')
        bgLog(`${bg.name}: no source would play (${can}).${CODEC_FIX}`)
        if (bg.name !== 'nebula') hud.bgName = 'nebula' // don't leave him on a black screen
        return
      }
      const src = bg.sources[i++]
      watch.current = { t: -1, at: performance.now(), ok: false }
      video.src = src
      video.play().catch((e) => {
        if (e?.name !== 'NotSupportedError' && e?.name !== 'AbortError') bgLog(`${src}: play() refused: ${e}`) // (errors are logged by onerror)
      })
    }
    nextRef.current = next
    video.onerror = () => {
      const e = video.error
      bgLog(`${video.currentSrc || video.src}: error ${e?.code ?? '?'} ${e?.message ?? ''} — trying the next file`)
      next()
    }
    // Belt and braces for webviews that don't honour video.loop (WebView2 on Windows):
    // if 'ended' fires at all, restart from the top ourselves.
    video.onended = () => {
      video.currentTime = 0
      video.play().catch(() => {})
    }
    next()
  }

  useEffect(() => {
    load(backgroundByName(hud.bgName))
    return () => {
      video.onerror = null
      video.onended = null
      video.pause()
      video.removeAttribute('src')
      video.load()
      texRef.current?.dispose()
      texRef.current = null
      scene.background = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useFrame((_, dt) => {
    // Swap videos: fade out, load, fade back in.
    const wanted = hud.bgName
    const switching = wanted !== current.current
    const ready = video.readyState >= 2 && !switching
    const target = hud.bg && ready ? 1 : 0
    fade.current += (target - fade.current) * (1 - Math.exp(-dt * (switching ? 6 : 2)))
    if (switching && fade.current < 0.03) load(backgroundByName(wanted))
    const tex = texRef.current
    if (!tex) return
    if (fade.current < 0.01) {
      if (scene.background === tex) scene.background = null
      if (!hud.bg && !video.paused) video.pause()
      return
    }
    if (hud.bg && video.paused && !switching) video.play().catch(() => {})
    // Watchdog: a video that never starts or freezes moves on to the next file.
    if (hud.bg && !switching && !video.paused) {
      const now = performance.now()
      const w8 = watch.current
      if (video.currentTime !== w8.t) {
        if (w8.t >= 0 && !w8.ok) {
          w8.ok = true
          if (hud.bgMissing === current.current) hud.bgMissing = null
          bgLog(`${current.current} playing: ${video.currentSrc.split('/').pop()} ${video.videoWidth}x${video.videoHeight}`)
        }
        w8.t = video.currentTime
        w8.at = now
      } else if (now - w8.at > STALL_MS) {
        w8.at = now
        // A clip frozen on its last frame is a loop that didn't restart, not a source that can't
        // play. WebView2 does this routinely. Rewind it instead of throwing the file away.
        const dur = video.duration
        if (w8.ok && Number.isFinite(dur) && dur > 0 && video.currentTime >= dur - 0.5) {
          bgLog(`${current.current} stalled at the loop point (t=${video.currentTime.toFixed(2)}/${dur.toFixed(2)}) — rewinding`)
          // Seeking can throw if the element isn't seekable; never let one bad frame escape
          // this callback, because an error here would tear down the whole UI.
          try {
            video.currentTime = 0
            video.play().catch(() => {})
          } catch (err) {
            bgLog(`rewind failed (${err}) — trying the next file`)
            nextRef.current()
          }
        } else {
          bgLog(
            `${video.currentSrc.split('/').pop() || '?'} stuck (readyState ${video.readyState}, network ${video.networkState}, t=${video.currentTime.toFixed(2)}) — trying the next file`,
          )
          nextRef.current()
        }
      }
    }
    const bg = backgroundByName(current.current)
    const w = canvas.width
    const h = canvas.height
    ctx.globalCompositeOperation = 'source-over'
    ctx.globalAlpha = 1
    ctx.fillStyle = '#000'
    ctx.fillRect(0, 0, w, h)
    ctx.globalAlpha = (1 - bg.dim) * fade.current
    if (video.readyState >= 2) ctx.drawImage(video, 0, 0, w, h)
    // Devil mode: multiply toward red.
    const d = devil.current ?? 0
    if (d > 0.01) {
      ctx.globalAlpha = 1
      ctx.globalCompositeOperation = 'multiply'
      const gb = Math.round(255 - 215 * d)
      ctx.fillStyle = `rgb(255,${gb},${gb})`
      ctx.fillRect(0, 0, w, h)
    }
    tex.needsUpdate = true
    scene.background = tex
  })

  return null
}
