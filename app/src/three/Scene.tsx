import { OrbitControls } from '@react-three/drei'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { Bloom, ChromaticAberration, EffectComposer, Noise, Vignette } from '@react-three/postprocessing'
import { useEffect, useMemo, useRef } from 'react'
import * as THREE from 'three'
import { audio } from '../lib/audio'
import type { EyeState, Mode } from '../types'
import DataArcs from './DataArcs'
import DotCore from './DotCore'
import VideoBackground from './VideoBackground'
import { eyeFragment, eyeVertex } from './eyeShader'

/** Palettes. Normal = warm orange-red accent on white; Devil = blood red everything. */
const PALETTE = {
  normal: { accent: new THREE.Color('#ff4a1c'), line: new THREE.Color('#e9edf2'), star: new THREE.Color('#ffffff'), bg: new THREE.Color('#000000') },
  devil: { accent: new THREE.Color('#ff0820'), line: new THREE.Color('#ff3a3a'), star: new THREE.Color('#ff6a6a'), bg: new THREE.Color('#070000') },
}

/** Per-state targets the scene eases toward. */
const TARGETS: Record<EyeState, { speed: number; pupil: number; energy: number; warp: number }> = {
  idle: { speed: 1, pupil: 0.3, energy: 0.12, warp: 1 },
  listening: { speed: 1.3, pupil: 0.26, energy: 0.3, warp: 1.2 },
  thinking: { speed: 4.5, pupil: 0.17, energy: 0.85, warp: 5 },
  speaking: { speed: 1.6, pupil: 0.34, energy: 0.55, warp: 1.6 },
  offline: { speed: 0.15, pupil: 0.45, energy: 0, warp: 0.2 },
}

// Mouse position in [-1, 1], tracked on window so HUD overlays don't block it.
const mouse = { x: 0, y: 0 }
if (typeof window !== 'undefined') {
  window.addEventListener('pointermove', (e) => {
    mouse.x = (e.clientX / window.innerWidth) * 2 - 1
    mouse.y = -((e.clientY / window.innerHeight) * 2 - 1)
  })
}

type Live = {
  speed: number
  pupil: number
  energy: number
  warp: number
  level: number
  devil: number // 0..1 blend into devil mode
  accent: THREE.Color
  line: THREE.Color
  star: THREE.Color
  bg: THREE.Color
}

function useLive(state: EyeState, mode: Mode) {
  const live = useRef<Live>({
    ...TARGETS.idle,
    level: 0,
    devil: 0,
    accent: PALETTE.normal.accent.clone(),
    line: PALETTE.normal.line.clone(),
    star: PALETTE.normal.star.clone(),
    bg: PALETTE.normal.bg.clone(),
  })
  useFrame((_, dt) => {
    const t = TARGETS[state]
    const l = live.current
    const k = 1 - Math.exp(-dt * 3)
    const d = mode === 'devil' ? 1 : 0
    l.devil += (d - l.devil) * (1 - Math.exp(-dt * 4))
    // Devil mode runs everything hotter.
    l.speed += (t.speed * (1 + l.devil * 1.2) - l.speed) * k
    l.pupil += (t.pupil * (1 - l.devil * 0.15) - l.pupil) * k
    l.energy += (Math.min(1, t.energy + l.devil * 0.35) - l.energy) * k
    l.warp += (t.warp * (1 + l.devil) - l.warp) * k
    l.accent.copy(PALETTE.normal.accent).lerp(PALETTE.devil.accent, l.devil)
    l.line.copy(PALETTE.normal.line).lerp(PALETTE.devil.line, l.devil)
    l.star.copy(PALETTE.normal.star).lerp(PALETTE.devil.star, l.devil)
    l.bg.copy(PALETTE.normal.bg).lerp(PALETTE.devil.bg, l.devil)
    // Real audio level from the brain when it's flowing; synthetic only if speaking without audio.
    const now = performance.now()
    const fresh = now - audio.at < 300
    const tt = now / 1000
    let target = 0
    if (fresh) target = audio.source === 'mic' ? audio.level * 0.6 : audio.level
    else if (state === 'speaking') target = 0.35 + 0.65 * Math.abs(Math.sin(tt * 9) * Math.sin(tt * 3.7 + 1))
    l.level += (target - l.level) * (1 - Math.exp(-dt * 18))
  })
  return live
}

/* ───────────────────────────── Eye ───────────────────────────── */

function Eyeball({ live }: { live: React.RefObject<Live> }) {
  const group = useRef<THREE.Group>(null!)
  const mat = useMemo(
    () =>
      new THREE.ShaderMaterial({
        vertexShader: eyeVertex,
        fragmentShader: eyeFragment,
        uniforms: {
          uTime: { value: 0 },
          uPupil: { value: 0.3 },
          uEnergy: { value: 0.15 },
          uSlit: { value: 0 },
          uAccent: { value: new THREE.Color() },
        },
      }),
    [],
  )

  useFrame(({ clock }, dt) => {
    const l = live.current
    mat.uniforms.uTime.value = clock.elapsedTime
    mat.uniforms.uPupil.value = l.pupil + l.level * 0.06 + Math.sin(clock.elapsedTime * 0.8) * 0.01
    mat.uniforms.uEnergy.value = l.energy + l.level * 0.5
    mat.uniforms.uSlit.value = l.devil
    ;(mat.uniforms.uAccent.value as THREE.Color).copy(l.accent)
    // Look at the cursor, with a little idle saccade. Devil mode tracks harder.
    const sx = Math.sin(clock.elapsedTime * 0.37) * 0.04
    const sy = Math.cos(clock.elapsedTime * 0.29) * 0.03
    const k = 1 - Math.exp(-dt * (6 + l.devil * 8))
    group.current.rotation.y += (mouse.x * 0.55 + sx - group.current.rotation.y) * k
    group.current.rotation.x += (-mouse.y * 0.4 + sy - group.current.rotation.x) * k
    // The eye only exists in devil mode: it opens as the core collapses.
    const s = Math.max(0.0001, l.devil)
    group.current.scale.setScalar(s)
    group.current.visible = s > 0.01
  })

  return (
    <group ref={group} scale={0.0001} visible={false}>
      <mesh material={mat}>
        <sphereGeometry args={[1, 128, 128]} />
      </mesh>
    </group>
  )
}

/* ─────────────────────────── Rings ─────────────────────────── */

function Gimbal({ live }: { live: React.RefObject<Live> }) {
  const a = useRef<THREE.Group>(null!)
  const b = useRef<THREE.Group>(null!)
  const ringMat = useMemo(() => new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.22, toneMapped: false }), [])
  const markMat = useMemo(() => new THREE.MeshBasicMaterial({ toneMapped: false }), [])
  useFrame((_, dt) => {
    const l = live.current
    a.current.rotation.y += 0.3 * l.speed * dt
    b.current.rotation.x -= 0.22 * l.speed * dt
    ringMat.color.copy(l.line)
    markMat.color.copy(l.accent)
  })
  const ring = (r: number) => (
    <>
      <mesh material={ringMat}>
        <torusGeometry args={[r, 0.003, 6, 256]} />
      </mesh>
      {[0, 2.1, 4.2].map((ang) => (
        <mesh key={ang} material={markMat} position={[Math.cos(ang) * r, Math.sin(ang) * r, 0]}>
          <sphereGeometry args={[0.02, 10, 10]} />
        </mesh>
      ))}
    </>
  )
  return (
    <>
      <group ref={a} rotation={[1.15, 0.25, 0]}>
        {ring(2.45)}
      </group>
      <group ref={b} rotation={[0.4, 0.95, 0.3]}>
        {ring(2.6)}
      </group>
    </>
  )
}

function Rings({ live }: { live: React.RefObject<Live> }) {
  const root = useRef<THREE.Group>(null!)
  useFrame((_, dt) => {
    // Gentle parallax against the cursor so the rig feels physical.
    const k = 1 - Math.exp(-dt * 2)
    root.current.rotation.y += (mouse.x * 0.22 - root.current.rotation.y) * k
    root.current.rotation.x += (-mouse.y * 0.15 - root.current.rotation.x) * k
  })
  return (
    <group ref={root}>
      <DataArcs live={live} accent={live.current.accent} lineColor={live.current.line} />
      <Gimbal live={live} />
    </group>
  )
}

/* ─────────────────────────── Voice ring ─────────────────────────── */

const BARS = 128

function VoiceRing({ live }: { live: React.RefObject<Live> }) {
  const ref = useRef<THREE.InstancedMesh>(null!)
  const m = useMemo(() => new THREE.Object3D(), [])
  const levels = useMemo(() => new Float32Array(BARS), [])
  const mat = useMemo(() => new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.85, toneMapped: false }), [])

  useFrame(({ clock, camera }) => {
    const l = live.current.level
    const t = clock.elapsedTime
    mat.color.copy(live.current.line)
    for (let i = 0; i < BARS; i++) {
      const a = (i / BARS) * Math.PI * 2
      const wave = Math.abs(Math.sin(a * 3 + t * 6) * Math.sin(a * 7 - t * 4))
      const target = 0.015 + l * (0.12 + wave * 0.45)
      levels[i] += (target - levels[i]) * 0.3
      const h = levels[i]
      const r = 1.08 + h / 2
      m.position.set(Math.cos(a) * r, Math.sin(a) * r, 0)
      m.rotation.set(0, 0, a - Math.PI / 2)
      m.scale.set(0.01, h, 0.004)
      m.updateMatrix()
      ref.current.setMatrixAt(i, m.matrix)
    }
    ref.current.instanceMatrix.needsUpdate = true
    ref.current.quaternion.copy(camera.quaternion)
  })

  return (
    <instancedMesh ref={ref} args={[undefined, undefined, BARS]} material={mat}>
      <boxGeometry args={[1, 1, 1]} />
    </instancedMesh>
  )
}

/* ─────────────────────────── Space ─────────────────────────── */

function makeShell(count: number, rMin: number, rMax: number) {
  const pos = new Float32Array(count * 3)
  const col = new Float32Array(count * 3)
  for (let i = 0; i < count; i++) {
    const u = Math.random() * 2 - 1
    const th = Math.random() * Math.PI * 2
    const s = Math.sqrt(1 - u * u)
    const r = rMin + Math.pow(Math.random(), 0.6) * (rMax - rMin)
    pos.set([s * Math.cos(th) * r, s * Math.sin(th) * r, u * r], i * 3)
    const b = 0.2 + Math.pow(Math.random(), 3) * 0.8
    col.set([b, b, b], i * 3)
  }
  const g = new THREE.BufferGeometry()
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3))
  g.setAttribute('color', new THREE.BufferAttribute(col, 3))
  return g
}

function Space({ live }: { live: React.RefObject<Live> }) {
  const far = useRef<THREE.Points>(null!)
  const near = useRef<THREE.Points>(null!)
  const farGeo = useMemo(() => makeShell(9000, 25, 120), [])
  const nearGeo = useMemo(() => makeShell(900, 4, 16), [])
  const { gl } = useThree()

  useFrame((_, dt) => {
    const l = live.current
    far.current.rotation.y += 0.004 * l.warp * dt
    far.current.rotation.x += 0.0015 * l.warp * dt
    near.current.rotation.y -= 0.02 * l.warp * dt
    near.current.rotation.z += 0.008 * l.warp * dt
    ;(far.current.material as THREE.PointsMaterial).color.copy(l.star)
    ;(near.current.material as THREE.PointsMaterial).color.copy(l.star)
    gl.setClearColor(l.bg)
    const k = 1 - Math.exp(-dt * 1.5)
    near.current.position.x += (-mouse.x * 0.6 - near.current.position.x) * k
    near.current.position.y += (-mouse.y * 0.4 - near.current.position.y) * k
  })

  return (
    <>
      <points ref={far} geometry={farGeo}>
        <pointsMaterial size={0.18} sizeAttenuation vertexColors transparent opacity={0.9} depthWrite={false} />
      </points>
      <points ref={near} geometry={nearGeo}>
        <pointsMaterial size={0.035} sizeAttenuation vertexColors transparent opacity={0.6} depthWrite={false} />
      </points>
    </>
  )
}

/* ─────────────────────────── Camera ─────────────────────────── */

function CameraRig({ booted, work }: { booted: boolean; work: boolean }) {
  const { camera } = useThree()
  const flying = useRef(true)
  const controls = useRef<any>(null)
  const pan = useRef(0)

  useEffect(() => {
    camera.position.set(0, 0, 60)
  }, [camera])

  useFrame((_, dt) => {
    // Work mode: slide the whole rig off to the left by panning camera + orbit target right.
    const want = work ? 16 : 0
    const next = pan.current + (want - pan.current) * (1 - Math.exp(-dt * 2.2))
    const d = next - pan.current
    pan.current = next
    if (Math.abs(d) > 1e-5) {
      camera.position.x += d
      if (controls.current) controls.current.target.x += d
    }
    if (!flying.current) return
    const targetZ = booted ? 7.6 : 45
    const k = 1 - Math.exp(-dt * (booted ? 1.6 : 0.4))
    camera.position.z += (targetZ - camera.position.z) * k
    camera.position.x += (pan.current - camera.position.x) * k
    camera.position.y += (0 - camera.position.y) * k
    if (booted && Math.abs(camera.position.z - targetZ) < 0.02) flying.current = false
  })

  return (
    <OrbitControls
      ref={controls}
      enabled={booted && !work}
      enablePan={false}
      enableDamping
      dampingFactor={0.06}
      rotateSpeed={0.5}
      minDistance={3.5}
      maxDistance={11}
      zoomSpeed={0.6}
    />
  )
}

const devilRefs = new WeakMap<object, { current: number }>()
/** A ref-like view of live.devil so the background can tint itself. */
function devilRef(live: React.RefObject<Live>) {
  let r = devilRefs.get(live)
  if (!r) {
    r = {
      get current() {
        return live.current.devil
      },
    }
    devilRefs.set(live, r)
  }
  return r as React.RefObject<number>
}

function Rig({ state, mode, booted, work }: { state: EyeState; mode: Mode; booted: boolean; work: boolean }) {
  const live = useLive(state, mode)
  return (
    <>
      <CameraRig booted={booted} work={work} />
      <VideoBackground devil={devilRef(live)} />
      <Space live={live} />
      <DotCore live={live} mouse={mouse} />
      <Eyeball live={live} />
      <Rings live={live} />
      <VoiceRing live={live} />
    </>
  )
}

export default function Scene({ state, mode, booted, work = false }: { state: EyeState; mode: Mode; booted: boolean; work?: boolean }) {
  const devil = mode === 'devil'
  return (
    <div className="scene">
      <Canvas
        linear
        dpr={[1, 2]}
        camera={{ fov: 42, near: 0.1, far: 400, position: [0, 0, 60] }}
        gl={{ antialias: true, powerPreference: 'high-performance' }}
      >
        <Rig state={state} mode={mode} booted={booted} work={work} />
        <EffectComposer multisampling={0}>
          <Bloom mipmapBlur intensity={devil ? 2.3 : 1.0} luminanceThreshold={devil ? 0.12 : 0.2} luminanceSmoothing={0.3} radius={devil ? 0.9 : 0.75} />
          <ChromaticAberration
            offset={devil ? new THREE.Vector2(0.0026, 0.0018) : new THREE.Vector2(0.0002, 0.0002)}
            radialModulation={false}
            modulationOffset={0}
          />
          <Noise opacity={devil ? 0.12 : 0.03} />
          <Vignette eskil={false} offset={devil ? 0.12 : 0.2} darkness={devil ? 1.25 : 0.85} />
        </EffectComposer>
      </Canvas>
    </div>
  )
}
