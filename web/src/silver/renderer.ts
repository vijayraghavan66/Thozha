import type { IdentityState } from '../components/motion';

const vertex = `
precision highp float;
attribute vec2 a_surface;
uniform float u_time, u_energy, u_opening, u_rotation, u_aspect;
varying vec3 v_normal, v_position;
varying vec2 v_surface;
vec3 center(float u) {
  float wave = sin(3.0*u + u_time)*u_energy;
  return vec3(
    (1.06 + .105*wave)*cos(u),
    (.64 + .09*wave)*sin(u),
    .30*sin(2.0*u + u_time*.34)*u_opening
  );
}
vec3 surface(vec2 uv) {
  float u = uv.x * 6.2831853;
  float v = uv.y * 6.2831853;
  vec3 tangent = normalize(center(u+.002)-center(u-.002));
  vec3 normal = normalize(cross(tangent, vec3(0.,0.,1.)));
  vec3 binormal = normalize(cross(normal, tangent));
  float radius = .205 + .05*sin(2.0*u-u_time*.65)*u_energy;
  return center(u) + radius*(cos(v)*normal + sin(v)*binormal*.78);
}
vec3 rotate(vec3 p) {
  float x = .46 + .15*sin(u_time*.3);
  float y = .2*sin(u_rotation);
  float z = -.16 + .10*sin(u_rotation*.6);
  p.yz = mat2(cos(x),-sin(x),sin(x),cos(x))*p.yz;
  p.xz = mat2(cos(y),-sin(y),sin(y),cos(y))*p.xz;
  p.xy = mat2(cos(z),-sin(z),sin(z),cos(z))*p.xy;
  return p;
}
void main() {
  vec3 p = surface(a_surface);
  vec3 du = surface(a_surface+vec2(.0005,0.))-p;
  vec3 dv = surface(a_surface+vec2(0.,.0005))-p;
  v_normal = normalize(rotate(cross(du,dv)));
  p = rotate(p);
  v_position = p;
  v_surface = a_surface;
  float perspective = 3.8/(3.8-p.z);
  float scale = min(.94,u_aspect*.53);
  gl_Position = vec4(p.x*scale/u_aspect*perspective, p.y*scale*perspective, -p.z*.15, 1.);
}`;

const fragment = `
precision highp float;
uniform float u_time, u_mode, u_resolution;
varying vec3 v_normal, v_position;
varying vec2 v_surface;
void main() {
  if (u_mode > 4.5 && u_mode < 5.5 && v_surface.x > .46 && v_surface.x < .49) discard;
  vec3 n = normalize(v_normal);
  if (!gl_FrontFacing) n = -n;
  vec3 eye = normalize(vec3(0.,0.,3.8)-v_position);
  vec3 r = reflect(-eye,n);
  float top = smoothstep(-.4,.85,r.y);
  float softbox = pow(max(0.,dot(r,normalize(vec3(-.5,1.,1.)))),18.);
  float strip = pow(max(0.,1.-abs(r.x*.75+r.z*.45+.12)),28.);
  float rim = pow(1.-max(0.,dot(n,eye)),3.);
  float bounce = pow(max(0.,dot(r,normalize(vec3(.8,-.4,.6)))),10.);
  vec3 metal = vec3(.034,.039,.047) + vec3(.24,.255,.28)*top
    + vec3(.82,.85,.9)*softbox + vec3(.85,.87,.9)*strip
    + vec3(.30,.32,.35)*rim + vec3(.18,.19,.21)*bounce;
  float current = pow(max(0.,cos(v_surface.x*6.2831853-u_time*1.8)),18.);
  if (u_mode > 1.5 && u_mode < 2.5) metal += vec3(.3)*current;
  if (u_mode > 2.5 && u_mode < 3.5) metal += vec3(.20)*pow(abs(n.y),10.);
  if (u_mode > 3.5 && u_mode < 4.5) metal += vec3(.25)*u_resolution;
  if (u_mode > 4.5 && u_mode < 5.5) metal *= vec3(.82,.73,.75);
  if (u_mode > 5.5) metal *= .5;
  gl_FragColor = vec4(pow(metal,vec3(.82)),1.);
}`;

const modes: Record<
  IdentityState,
  { mode: number; speed: number; energy: number; opening: number }
> = {
  idle: { mode: 0, speed: 0.8, energy: 0.9, opening: 1 },
  thinking: { mode: 1, speed: 1.65, energy: 1.7, opening: 1.35 },
  executing: { mode: 2, speed: 1.2, energy: 1, opening: 0.65 },
  approval: { mode: 3, speed: 0.35, energy: 0.45, opening: 1.8 },
  completed: { mode: 4, speed: 0.25, energy: 0.2, opening: 0.2 },
  failed: { mode: 5, speed: 0.12, energy: 0.12, opening: 0.7 },
  cancelled: { mode: 6, speed: 0.12, energy: 0.1, opening: 0.3 },
};

export interface SilverRenderer {
  setState: (state: IdentityState) => void;
  setActive: (active: boolean) => void;
  dispose: () => void;
}

export function mountSilver(canvas: HTMLCanvasElement, host: HTMLElement): SilverRenderer {
  const gl = canvas.getContext('webgl', {
    alpha: true,
    antialias: true,
    depth: true,
    powerPreference: 'low-power',
  });
  if (!gl) throw new Error('WebGL is unavailable');
  const shaders: WebGLShader[] = [];
  const buffers: WebGLBuffer[] = [];
  let program: WebGLProgram | null = null;
  function release() {
    buffers.forEach(buffer => gl!.deleteBuffer(buffer));
    shaders.forEach(shader => gl!.deleteShader(shader));
    if (program) gl!.deleteProgram(program);
  }
  try {
    for (const [type, source] of [
      [gl.VERTEX_SHADER, vertex],
      [gl.FRAGMENT_SHADER, fragment],
    ] as const) {
      const shader = gl.createShader(type);
      if (!shader) throw new Error('Could not allocate silver shader');
      shaders.push(shader);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS))
        throw new Error(gl.getShaderInfoLog(shader) || 'Silver shader compilation failed');
    }
    program = gl.createProgram();
    if (!program) throw new Error('Could not allocate silver material');
    shaders.forEach(shader => gl.attachShader(program!, shader));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS))
      throw new Error(gl.getProgramInfoLog(program) || 'Silver material linking failed');
    gl.useProgram(program);
    const segments = 144,
      sides = 24;
    const vertices: number[] = [],
      indices: number[] = [];
    for (let u = 0; u <= segments; u++)
      for (let v = 0; v <= sides; v++) vertices.push(u / segments, v / sides);
    for (let u = 0; u < segments; u++)
      for (let v = 0; v < sides; v++) {
        const a = u * (sides + 1) + v,
          b = a + sides + 1;
        indices.push(a, b, a + 1, b, b + 1, a + 1);
      }
    for (const [target, data] of [
      [gl.ARRAY_BUFFER, new Float32Array(vertices)],
      [gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(indices)],
    ] as const) {
      const buffer = gl.createBuffer();
      if (!buffer) throw new Error('Could not allocate silver mesh');
      buffers.push(buffer);
      gl.bindBuffer(target, buffer);
      gl.bufferData(target, data, gl.STATIC_DRAW);
    }
    const position = gl.getAttribLocation(program, 'a_surface');
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    const uniforms = Object.fromEntries(
      ['time', 'energy', 'opening', 'rotation', 'aspect', 'mode', 'resolution'].map(name => [
        name,
        gl.getUniformLocation(program!, `u_${name}`),
      ])
    );
    gl.enable(gl.DEPTH_TEST);
    gl.clearColor(0, 0, 0, 0);
    let active = true,
      visible = true,
      disposed = false,
      lost = false;
    let raf = 0,
      last = 0,
      time = 0,
      changed = performance.now();
    let state: IdentityState = 'idle';
    let energy = 0.9,
      opening = 1;
    const preference = matchMedia('(prefers-reduced-motion: reduce)');
    const root = host.closest('.th-workspace');
    const reduced = () => preference.matches || Boolean(root?.classList.contains('th-stop-motion'));
    const eligible = () => active && visible && !document.hidden && !disposed && !lost;
    const settled = () =>
      ['completed', 'failed', 'cancelled'].includes(state) && performance.now() - changed > 1200;
    function draw(now: number) {
      if (!eligible()) return;
      const dt = last ? Math.min((now - last) / 1000, 0.05) : 0;
      last = now;
      const target = modes[state];
      time += reduced() ? 0 : dt * target.speed;
      const blend = reduced() ? 1 : 1 - Math.exp(-dt * 5);
      energy += (target.energy - energy) * blend;
      opening += (target.opening - opening) * blend;
      gl!.uniform1f(uniforms.time, time);
      gl!.uniform1f(uniforms.rotation, time * 0.38);
      gl!.uniform1f(uniforms.energy, energy);
      gl!.uniform1f(uniforms.opening, opening);
      gl!.uniform1f(uniforms.mode, target.mode);
      gl!.uniform1f(uniforms.resolution, reduced() ? 0 : Math.max(0, 1 - (now - changed) / 900));
      gl!.clear(gl!.COLOR_BUFFER_BIT | gl!.DEPTH_BUFFER_BIT);
      gl!.drawElements(gl!.TRIANGLES, indices.length, gl!.UNSIGNED_SHORT, 0);
    }
    function frame(now: number) {
      if (!eligible() || reduced() || settled()) {
        sync();
        return;
      }
      raf = requestAnimationFrame(frame);
      if (now - last >= 16) draw(now);
    }
    function sync() {
      cancelAnimationFrame(raf);
      raf = 0;
      last = 0;
      host.dataset.motion = !eligible()
        ? 'paused'
        : reduced()
          ? 'static'
          : settled()
            ? 'settled'
            : 'running';
      if (!eligible()) return;
      draw(performance.now());
      if (!reduced() && !settled()) raf = requestAnimationFrame(frame);
    }
    function resize() {
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const ratio = Math.min(devicePixelRatio || 1, 1.5, 720 / rect.width, 420 / rect.height);
      canvas.width = Math.max(1, Math.round(rect.width * ratio));
      canvas.height = Math.max(1, Math.round(rect.height * ratio));
      gl!.viewport(0, 0, canvas.width, canvas.height);
      gl!.uniform1f(uniforms.aspect, rect.width / rect.height);
      sync();
    }
    function contextLost(event: Event) {
      event.preventDefault();
      lost = true;
      host.dataset.renderer = 'fallback';
      console.warn('Thozha silver: graphics context lost; displaying the static identity.');
      sync();
    }
    const size = new ResizeObserver(resize);
    size.observe(canvas);
    const intersection = new IntersectionObserver(entries => {
      visible = entries[0].isIntersecting;
      sync();
    });
    intersection.observe(canvas);
    const settings = new MutationObserver(sync);
    if (root) settings.observe(root, { attributes: true, attributeFilter: ['class'] });
    document.addEventListener('visibilitychange', sync);
    preference.addEventListener('change', sync);
    canvas.addEventListener('webglcontextlost', contextLost);
    host.dataset.renderer = 'webgl';
    resize();
    return {
      setState(next) {
        if (next === state) return;
        state = next;
        changed = performance.now();
        sync();
      },
      setActive(next) {
        if (next === active) return;
        active = next;
        sync();
      },
      dispose() {
        disposed = true;
        cancelAnimationFrame(raf);
        size.disconnect();
        intersection.disconnect();
        settings.disconnect();
        document.removeEventListener('visibilitychange', sync);
        preference.removeEventListener('change', sync);
        canvas.removeEventListener('webglcontextlost', contextLost);
        release();
      },
    };
  } catch (error) {
    release();
    throw error;
  }
}
