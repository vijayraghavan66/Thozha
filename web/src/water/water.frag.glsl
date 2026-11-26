#version 300 es
// Adapted from Jarvis Water Lab's water.glsl at 2dcf244.
precision highp float;
out vec4 outColor;
uniform vec2 resolution;
uniform float time;
uniform float lean;
uniform vec4 ripples[4];

const float IOR = 1.333;

float wave(vec2 p) {
    float value = 0.0;
    for (int i = 0; i < 4; i++) {
        vec4 ripple = ripples[i];
        if (ripple.w == 0.0) continue;
        float age = ripple.z;
        float front = distance(p, ripple.xy) - age * 1.3;
        value += ripple.w * sin(front * 26.0) *
            exp(-front * front * 6.0 - age * 1.3) * smoothstep(0.0, 0.06, age);
    }
    return value;
}

float heightAt(vec2 p, float t) {
    float swell = sin(p.x * 3.8 + t) * cos(p.y * 3.4 - t * 0.7) * 0.035;
    float crossing = sin(dot(p, vec2(-2.9, 7.4)) + t * 0.84 + 0.63 * sin(p.x * 1.85 - p.y * 3.1));
    float capillary = sin(dot(p, vec2(4.8, 13.4)) - t * 1.21 + 1.17 * sin(p.x * 3.4 + p.y * 2.8));
    return swell + 0.027 * crossing + 0.0085 * capillary + wave(p);
}

void main() {
    vec2 uv = gl_FragCoord.xy / resolution;
    float depth = 1.0 / (0.55 + (1.0 - uv.y) * 0.45);
    vec2 p = vec2((uv.x - 0.5) * resolution.x / resolution.y, uv.y * 1.25) * depth;
    float height = heightAt(p, time * 0.83);
    // Convert screen derivatives to the slope in water space, independent of buffer size.
    float slopeX = dFdx(height) / max(dFdx(p.x), 0.00001);
    float slopeZ = (dFdy(height) - dFdy(p.x) * slopeX) / max(dFdy(p.y), 0.00001);
    vec3 n = normalize(vec3(-slopeX * 1.25, 1.0, -slopeZ));
    vec3 view = normalize(vec3((0.60 - uv.x) * 0.28, 0.61 - uv.y * 0.20, -0.87));
    vec3 reflected = reflect(-view, n);
    vec3 ray = refract(-view, n, 1.0 / IOR);
    float cosine = clamp(dot(view, n), 0.0, 1.0);
    float fresnel = 0.02037 + 0.97963 * pow(1.0 - cosine, 5.0);

    // Distort only the embedded graphite bed, never the actual DOM or text.
    vec2 bed = p + ray.xz * (0.39 - height);
    float floorLight = 0.5 + 0.5 * sin(bed.y * 6.2 - bed.x * 1.7 + sin(bed.x * 2.4));
    float seam = pow(0.5 + 0.5 * sin(bed.y * 15.0 + bed.x * 2.1), 9.0);
    float transmission = (0.105 + floorLight * 0.057 + seam * 0.017) * exp(-0.76 * (0.34 - height));
    float strip = exp(-pow(abs(reflected.y - 0.55 + (reflected.x + lean * 0.09) * 0.42) * 7.6, 2.0));
    float crest = abs(reflected.y - 0.61 + reflected.x * 0.46);
    float pearl = exp(-pow(crest * 23.0, 2.0));
    float silver = exp(-pow(crest * 46.0, 2.0));
    float opening = smoothstep(-0.64, 0.47, reflected.x + 0.21 * reflected.z + lean * 0.12);
    float reflection = 0.07 + opening * (strip * 1.85 + pearl * 0.84);
    float grazing = pow(1.0 - cosine, 2.0);
    float tone = mix(transmission, reflection, fresnel) +
        opening * (strip * 0.14 + pearl * 0.16 + silver * 0.12) * (0.6 + grazing);
    float light = (0.65 + 0.35 * smoothstep(0.06, 0.95, uv.x)) *
        (0.63 + 0.37 * smoothstep(0.0, 0.23, uv.y));
    tone = 0.035 + (1.0 - exp(-tone * 1.2)) * light;
    outColor = vec4(vec3(tone * 0.99, tone, tone * 1.02), 1.0);
}
