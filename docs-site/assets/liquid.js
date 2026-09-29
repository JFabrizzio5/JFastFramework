/* The liquid glass ribbon behind the landing page.
 *
 * Two twisted elliptical tubes built on the GPU, lit by reflections of a
 * procedural photo studio and refracted through a ruby-tinted body. three.js
 * comes from cdnjs with an integrity hash; if it is blocked, or WebGL is not
 * available, the page adds `no-webgl` to <html> and CSS paints a still ruby
 * glow instead. Nothing on the page depends on this file running.
 */
(function () {
  "use strict";

  var root = document.documentElement;
  var canvas = document.getElementById("liquid");
  var glow = document.getElementById("liquid-glow");
  if (!canvas) return;

  function fallback() {
    root.classList.add("no-webgl");
    canvas.style.display = "none";
  }

  if (typeof THREE === "undefined") {
    fallback();
    return;
  }

  // Ruby. accent lights the fill and the edges; glow is the rim; body is the
  // tint of the liquid itself; deep is the shadow inside it.
  var RUBY = {
    accent: "#E11D2E",
    glow: "#FF4D5E",
    body: "#7A0714",
    deep: "#31060A",
    spec: "#FFE9EB"
  };

  var uniforms = {
    uTime: { value: 0 },
    uMouse: { value: new THREE.Vector2(0, 0) },
    uPulse: { value: 0 },
    uIsLight: { value: 0 },
    // A little under full: the ribbon is the backdrop, not the subject.
    uIntensity: { value: 0.82 },
    uAccent: { value: new THREE.Color(RUBY.accent) },
    uGlow: { value: new THREE.Color(RUBY.glow) },
    uBody: { value: new THREE.Color(RUBY.body) },
    uDeep: { value: new THREE.Color(RUBY.deep) },
    uSpec: { value: new THREE.Color(RUBY.spec) }
  };

  var VERT = [
    "uniform float uTime;",
    "uniform float uWidth;",
    "uniform float uThick;",
    "uniform float uTwist;",
    "uniform float uTwistSpeed;",
    "uniform float uWave;",
    "uniform float uSeed;",
    "uniform float uPulse;",
    "uniform vec2 uMouse;",
    "attribute vec3 aN;",
    "attribute vec3 aB;",
    "attribute vec2 aUV;",
    "varying vec3 vNormal;",
    "varying vec3 vViewPos;",
    "varying vec2 vUv;",
    "varying float vEdge;",
    "void main() {",
    "  float u = aUV.x;",
    "  float th = aUV.y;",
    "  float t = uTime;",
    "  float tw = u * uTwist + t * uTwistSpeed + sin(u * 6.2831 + t * 0.45 + uSeed) * 0.8;",
    "  float ct = cos(tw);",
    "  float st = sin(tw);",
    "  vec3 n = ct * aN + st * aB;",
    "  vec3 b = -st * aN + ct * aB;",
    "  float taper = smoothstep(0.0, 0.07, u) * (1.0 - smoothstep(0.93, 1.0, u));",
    "  float w = uWidth * (0.8 + 0.3 * sin(u * 7.0 - t * 0.6 + uSeed)) * (1.0 + uPulse * 0.22) * taper + 0.001;",
    "  float k = uThick * (0.85 + 0.25 * cos(u * 11.0 + t * 0.8 + uSeed)) * taper + 0.001;",
    "  vec3 flow = vec3(",
    "    sin(u * 5.0 + t * 0.55 + uSeed) * 0.45,",
    "    cos(u * 3.0 + t * 0.38 + uSeed) * 0.25,",
    "    sin(u * 4.0 - t * 0.50 + uSeed) * 0.55",
    "  ) * uWave * (1.0 + uPulse * 1.4);",
    "  float mid = 1.0 - abs(u - 0.5) * 2.0;",
    "  flow.x += uMouse.x * 0.9 * mid;",
    "  flow.y += uMouse.y * 0.6 * mid;",
    "  float c = cos(th);",
    "  float s = sin(th);",
    "  vec3 p = position + flow + n * (c * w) + b * (s * k);",
    "  vec3 nrm = normalize(n * (c / w) + b * (s / k));",
    "  vec4 mv = modelViewMatrix * vec4(p, 1.0);",
    "  vViewPos = mv.xyz;",
    "  vNormal = normalize(normalMatrix * nrm);",
    "  vUv = aUV;",
    "  vEdge = abs(c);",
    "  gl_Position = projectionMatrix * mv;",
    "}"
  ].join("\n");

  var FRAG = [
    "uniform vec3 uAccent;",
    "uniform vec3 uGlow;",
    "uniform vec3 uBody;",
    "uniform vec3 uDeep;",
    "uniform vec3 uSpec;",
    "uniform float uIsLight;",
    "uniform float uIntensity;",
    "uniform float uBackface;",
    "uniform float uPulse;",
    "uniform float uTime;",
    "varying vec3 vNormal;",
    "varying vec3 vViewPos;",
    "varying vec2 vUv;",
    "varying float vEdge;",
    "float strip(float x, float halfW, float soft) {",
    "  return 1.0 - smoothstep(halfW, halfW + soft, abs(x));",
    "}",
    "vec3 studioEnv(vec3 d) {",
    "  vec3 col = uDeep * 0.05;",
    "  float key  = strip(d.x - 0.62, 0.04, 0.12) * smoothstep(-0.4, 0.3, d.y);",
    "  float fill = strip(d.x + 0.58, 0.05, 0.16) * (1.0 - smoothstep(-0.3, 0.55, d.y));",
    "  float top  = strip(d.y - 0.80, 0.02, 0.10) * (1.0 - smoothstep(0.2, 0.95, abs(d.x)));",
    "  float rim  = exp(-pow((d.y + 0.15) * 4.5, 2.0)) * smoothstep(-0.3, 0.7, -d.z);",
    "  float back = pow(max(-d.z, 0.0), 3.0);",
    "  col += uSpec * (key * 1.3 + top * 0.5);",
    "  col += uAccent * fill * 2.1 + vec3(1.0) * pow(fill, 8.0) * 0.35;",
    "  col += uGlow * rim * 1.15;",
    "  col += mix(uDeep, uAccent, 0.4) * back * 0.6;",
    "  return col;",
    "}",
    "void main() {",
    "  vec3 N = normalize(vNormal);",
    "  vec3 V = normalize(vViewPos);",
    "  if (dot(N, V) > 0.0) N = -N;",
    "  float cosT = clamp(dot(-V, N), 0.0, 1.0);",
    "  float facing = 1.0 - cosT;",
    "  float fres = 0.04 + 0.96 * pow(facing, 4.0);",
    "  vec3 R = reflect(V, N);",
    "  vec3 refl = studioEnv(R);",
    "  float eta = 1.0 / 1.46;",
    "  float disp = 0.018;",
    "  vec3 refr = vec3(",
    "    studioEnv(refract(V, N, eta - disp)).r,",
    "    studioEnv(refract(V, N, eta)).g,",
    "    studioEnv(refract(V, N, eta + disp)).b",
    "  );",
    "  float thick = pow(facing, 1.4);",
    "  vec3 absorb = mix(vec3(1.0), uBody * 1.6, 0.45 + 0.55 * thick);",
    "  vec3 bodyCol = refr * absorb * 0.75;",
    "  float edge = pow(vEdge, 10.0);",
    "  float shimmer = 0.5 + 0.5 * sin(vUv.x * 18.0 - uTime * 1.3 + vUv.y);",
    "  bodyCol += uAccent * edge * (0.12 + 0.35 * shimmer);",
    "  bodyCol += uBody * (0.22 + thick * 0.6);",
    "  vec3 col = mix(bodyCol, refl, fres);",
    "  vec3 L1 = normalize(vec3(0.55, 0.7, 0.45));",
    "  vec3 L2 = normalize(vec3(-0.6, -0.25, 0.75));",
    "  float g1 = pow(max(dot(R, L1), 0.0), 160.0);",
    "  float g2 = pow(max(dot(R, L2), 0.0), 70.0);",
    "  col += uSpec * g1 * 2.4 + uGlow * g2 * 1.4;",
    "  col *= uIntensity * (1.0 + uPulse * 0.55);",
    "  float lum = dot(col, vec3(0.299, 0.587, 0.114));",
    "  float alpha = clamp(0.1 + fres * 0.75 + lum * 0.9, 0.0, 1.0);",
    "  if (uIsLight > 0.5) {",
    "    vec3 tint = mix(vec3(1.0), uAccent, 0.3);",
    "    vec3 lc = mix(tint, uDeep, clamp(pow(facing, 1.8) * 0.9, 0.0, 1.0));",
    "    lc = mix(lc, uAccent, edge * 0.5);",
    "    lc += uSpec * g1 * 1.2;",
    "    col = lc;",
    "    alpha = clamp((0.16 + pow(facing, 1.6) * 0.72 + g1 * 0.5) * uIntensity, 0.0, 0.95);",
    "  }",
    "  if (!gl_FrontFacing) alpha *= uBackface;",
    "  gl_FragColor = vec4(col, alpha);",
    "}"
  ].join("\n");

  var MAIN_SPINE = [
    [10.5, 14.0, -4.0], [7.5, 9.5, -1.0], [2.2, 6.0, 2.0], [1.2, 2.5, 3.0],
    [5.5, 0.0, 2.0], [10.0, -2.5, 0.0], [9.0, -6.5, -1.0], [4.0, -9.0, 1.0], [5.5, -14.0, -2.0]
  ];
  var SECONDARY_SPINE = [
    [13.5, 12.0, -6.0], [9.5, 8.5, -4.0], [5.0, 7.5, -2.5], [3.2, 4.0, -1.0],
    [7.2, 1.5, -2.0], [11.5, -1.0, -3.5], [12.0, -5.0, -4.0], [7.5, -8.0, -3.0], [9.0, -14.0, -5.0]
  ];

  var renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas: canvas,
      alpha: true,
      antialias: true,
      powerPreference: "high-performance"
    });
  } catch (e) {
    fallback();
    return;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);

  var scene = new THREE.Scene();
  var camera = new THREE.PerspectiveCamera(42, window.innerWidth / window.innerHeight, 0.1, 100);
  camera.position.set(0, 0, 22);
  var group = new THREE.Group();
  scene.add(group);

  function ribbonGeometry(points, segments, radial) {
    var curve = new THREE.CatmullRomCurve3(points, false, "centripetal");
    var frames = curve.computeFrenetFrames(segments, false);
    var count = (segments + 1) * (radial + 1);
    var pos = new Float32Array(count * 3);
    var nArr = new Float32Array(count * 3);
    var bArr = new Float32Array(count * 3);
    var uv = new Float32Array(count * 2);
    var v = 0;
    for (var i = 0; i <= segments; i++) {
      var u = i / segments;
      var p = curve.getPointAt(u);
      var N = frames.normals[i];
      var B = frames.binormals[i];
      for (var j = 0; j <= radial; j++) {
        pos[v * 3] = p.x; pos[v * 3 + 1] = p.y; pos[v * 3 + 2] = p.z;
        nArr[v * 3] = N.x; nArr[v * 3 + 1] = N.y; nArr[v * 3 + 2] = N.z;
        bArr[v * 3] = B.x; bArr[v * 3 + 1] = B.y; bArr[v * 3 + 2] = B.z;
        uv[v * 2] = u;
        uv[v * 2 + 1] = (j / radial) * Math.PI * 2;
        v++;
      }
    }
    var index = [];
    for (var a = 0; a < segments; a++) {
      for (var r = 0; r < radial; r++) {
        var i0 = a * (radial + 1) + r;
        var i1 = (a + 1) * (radial + 1) + r;
        index.push(i0, i0 + 1, i1, i1, i0 + 1, i1 + 1);
      }
    }
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    geo.setAttribute("aN", new THREE.BufferAttribute(nArr, 3));
    geo.setAttribute("aB", new THREE.BufferAttribute(bArr, 3));
    geo.setAttribute("aUV", new THREE.BufferAttribute(uv, 2));
    geo.setIndex(index);
    return geo;
  }

  // Inner faces first, then outer: the back of the glass shows through the
  // front, which is what reads as volume rather than as a flat ribbon.
  function addRibbon(cfg) {
    var geo = ribbonGeometry(
      cfg.spine.map(function (p) { return new THREE.Vector3(p[0], p[1], p[2]); }),
      cfg.segments,
      cfg.radial
    );
    var own = {
      uWidth: { value: cfg.width },
      uThick: { value: cfg.thickness },
      uTwist: { value: cfg.twist },
      uTwistSpeed: { value: cfg.twistSpeed },
      uWave: { value: cfg.wave },
      uSeed: { value: cfg.seed }
    };
    function material(side, backface) {
      return new THREE.ShaderMaterial({
        uniforms: Object.assign({}, uniforms, own, { uBackface: { value: backface } }),
        vertexShader: VERT,
        fragmentShader: FRAG,
        transparent: true,
        depthWrite: false,
        side: side
      });
    }
    var back = new THREE.Mesh(geo, material(THREE.BackSide, 0.55));
    var front = new THREE.Mesh(geo, material(THREE.FrontSide, 1.0));
    back.renderOrder = cfg.order;
    front.renderOrder = cfg.order + 1;
    back.frustumCulled = false;
    front.frustumCulled = false;
    group.add(back, front);
  }

  addRibbon({ spine: SECONDARY_SPINE, width: 0.95, thickness: 0.22, twist: 9.0, twistSpeed: -0.35, wave: 0.8, seed: 2.1, segments: 240, radial: 48, order: 0 });
  addRibbon({ spine: MAIN_SPINE, width: 1.75, thickness: 0.38, twist: 7.5, twistSpeed: 0.28, wave: 1.0, seed: 0.0, segments: 300, radial: 72, order: 2 });

  // Dust in the studio light.
  var dust = new Float32Array(360 * 3);
  for (var d = 0; d < 360; d++) {
    dust[d * 3] = (Math.random() - 0.5) * 44;
    dust[d * 3 + 1] = (Math.random() - 0.5) * 30;
    dust[d * 3 + 2] = (Math.random() - 0.5) * 16 - 4;
  }
  var dustGeo = new THREE.BufferGeometry();
  dustGeo.setAttribute("position", new THREE.BufferAttribute(dust, 3));
  var particles = new THREE.Points(dustGeo, new THREE.PointsMaterial({
    color: RUBY.glow,
    size: 0.055,
    transparent: true,
    opacity: 0.4,
    depthWrite: false,
    blending: THREE.AdditiveBlending
  }));
  scene.add(particles);

  var layout = { x: 0.6, scale: 1 };
  function resize() {
    var w = window.innerWidth;
    var h = window.innerHeight;
    var aspect = w / h;
    camera.aspect = aspect;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
    // On a narrow screen the ribbon sits behind the text: move it aside and
    // dim it, so the headline stays readable.
    if (aspect < 0.8) { layout.x = -2.4; layout.scale = 0.8; canvas.style.opacity = "0.4"; }
    else if (aspect < 1.25) { layout.x = -1.6; layout.scale = 0.9; canvas.style.opacity = "0.62"; }
    else { layout.x = 0.6; layout.scale = 1; canvas.style.opacity = "1"; }
    group.scale.setScalar(layout.scale);
  }
  resize();
  window.addEventListener("resize", resize);

  // The theme is an attribute on <html>; follow it without a reload.
  function syncTheme() {
    uniforms.uIsLight.value = root.getAttribute("data-theme") === "light" ? 1 : 0;
  }
  syncTheme();
  new MutationObserver(syncTheme).observe(root, { attributes: true, attributeFilter: ["data-theme"] });

  var mouse = { x: 0, y: 0, tx: 0, ty: 0 };
  window.addEventListener("pointermove", function (e) {
    mouse.tx = (e.clientX / window.innerWidth) * 2 - 1;
    mouse.ty = -(e.clientY / window.innerHeight) * 2 + 1;
    if (glow) {
      glow.style.left = e.clientX + "px";
      glow.style.top = e.clientY + "px";
    }
  }, { passive: true });

  var scroll = 0;
  window.addEventListener("scroll", function () {
    var max = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
    scroll = Math.min(1, Math.max(0, window.scrollY / max));
  }, { passive: true });

  // Reaching for a primary call to action sends a ripple through the glass.
  document.querySelectorAll("[data-pulse]").forEach(function (el) {
    el.addEventListener("pointerenter", function () { uniforms.uPulse.value = 1; });
  });

  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var last = performance.now();
  var time = 0;

  function tick(now) {
    requestAnimationFrame(tick);
    // A hidden tab is not worth a GPU.
    if (document.hidden) { last = now; return; }
    var dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    time += dt * (reduced ? 0.25 : 1);
    uniforms.uTime.value = time;
    uniforms.uPulse.value *= Math.exp(-1.8 * dt);

    var k = 1 - Math.exp(-dt * 3);
    mouse.x += (mouse.tx - mouse.x) * k;
    mouse.y += (mouse.ty - mouse.y) * k;
    uniforms.uMouse.value.set(mouse.x, mouse.y);

    group.position.set(layout.x + mouse.x * 0.4, scroll * 4.0 + mouse.y * 0.3, 0);
    group.rotation.set(-mouse.y * 0.08, Math.sin(time * 0.18) * 0.14 + mouse.x * 0.2, scroll * 0.22);
    particles.rotation.y = time * 0.012;
    particles.position.y = Math.sin(time * 0.2) * 0.4 + scroll * 2.0;

    renderer.render(scene, camera);
  }
  // One frame now, whatever the tab's state: a preview or a thumbnail of a
  // hidden tab should show the glass, not an empty page.
  renderer.render(scene, camera);
  requestAnimationFrame(tick);
})();
