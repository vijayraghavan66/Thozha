import fragment from './water.frag.glsl?raw';
import type { WaterMotion } from './motion';

// The fullscreen triangle is from Jarvis Water Lab's main.mjs at 2dcf244.
const vertex = `#version 300 es
void main() {
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;

function link(gl: WebGL2RenderingContext) {
  const program = gl.createProgram();
  if (!program) throw new Error('Could not allocate a water shader program.');
  const stages: WebGLShader[] = [];
  try {
    for (const [type, source] of [
      [gl.VERTEX_SHADER, vertex],
      [gl.FRAGMENT_SHADER, fragment],
    ] as const) {
      const shader = gl.createShader(type);
      if (!shader) throw new Error('Could not allocate a water shader.');
      stages.push(shader);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        throw new Error(gl.getShaderInfoLog(shader) || 'Water shader compilation failed.');
      }
      gl.attachShader(program, shader);
    }
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(gl.getProgramInfoLog(program) || 'Could not link water shaders.');
    }
    for (const shader of stages) gl.detachShader(program, shader);
    return program;
  } catch (error) {
    gl.deleteProgram(program);
    throw error;
  } finally {
    for (const shader of stages) gl.deleteShader(shader);
  }
}

export function createWaterRenderer(canvas: HTMLCanvasElement) {
  const gl = canvas.getContext('webgl2', {
    alpha: false,
    antialias: false,
    depth: false,
    stencil: false,
    powerPreference: 'low-power',
    failIfMajorPerformanceCaveat: true,
  });
  if (!gl) return undefined;
  const program = link(gl);
  const vao = gl.createVertexArray();
  if (!vao) {
    gl.deleteProgram(program);
    throw new Error('Could not allocate the water vertex array.');
  }
  const resolution = gl.getUniformLocation(program, 'resolution');
  const time = gl.getUniformLocation(program, 'time');
  const lean = gl.getUniformLocation(program, 'lean');
  const ripples = gl.getUniformLocation(program, 'ripples[0]');
  gl.bindVertexArray(vao);
  gl.useProgram(program);

  return {
    resize(width: number, height: number) {
      if (canvas.width !== width) canvas.width = width;
      if (canvas.height !== height) canvas.height = height;
      gl.viewport(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight);
      gl.uniform2f(resolution, gl.drawingBufferWidth, gl.drawingBufferHeight);
    },
    draw(motion: WaterMotion) {
      gl.uniform1f(time, motion.time);
      gl.uniform1f(lean, motion.lean);
      gl.uniform4fv(ripples, motion.ripples);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    },
    dispose() {
      gl.bindVertexArray(null);
      gl.useProgram(null);
      gl.deleteVertexArray(vao);
      gl.deleteProgram(program);
    },
  };
}
