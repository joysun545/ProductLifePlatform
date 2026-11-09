/* PLDP sea-slug parametric backdrop: one canvas, one clock, no navigation hooks. */
(() => {
  'use strict';

  if (window.__pldpSeaSlugBackground) return;
  const canvas = document.getElementById('pldp-sea-slug-background');
  if (!canvas) return;
  const context = canvas.getContext('2d', {alpha: true});
  if (!context) return;

  const POINT_COUNT = 20000;
  const SAMPLING_DIVISOR = 638;
  const LOOP_SECONDS = 32;
  const PHASE_PERIOD = Math.PI * 8;
  const MAX_BACKING_PIXELS = 2800000;
  const epoch = performance.now();

  // The image is a cloud of round points. Connecting consecutive samples
  // would add unrelated chords between its two folded wings.
  // Cache the static terms so a frame requires only six trigonometric calls.
  const constantRadius = new Float64Array(POINT_COUNT);
  const radialWeight = new Float64Array(POINT_COUNT);
  const distanceSquared = new Float64Array(POINT_COUNT);
  const sinDistance = new Float64Array(POINT_COUNT);
  const cosDistance = new Float64Array(POINT_COUNT);
  const sinDistanceSquared = new Float64Array(POINT_COUNT);
  const cosDistanceSquared = new Float64Array(POINT_COUNT);
  const sinBaseAngle = new Float64Array(POINT_COUNT);
  const cosBaseAngle = new Float64Array(POINT_COUNT);

  for (let index = 0; index < POINT_COUNT; index++) {
    const y = index / SAMPLING_DIVISOR;
    const k = (4 + Math.cos(y)) * Math.sin(index / 7) + 0.0001;
    const e = y / 6 - 9;
    const d = Math.hypot(k, e) - 3;
    const square = d * d;
    const weight = y * k / 13;
    const angle = d / 3 + 9 * (index % 2) + k * k / 59;
    constantRadius[index] = 99 + 3 * Math.sin(3 * k) + weight * e;
    radialWeight[index] = weight;
    distanceSquared[index] = square;
    sinDistance[index] = Math.sin(d);
    cosDistance[index] = Math.cos(d);
    sinDistanceSquared[index] = Math.sin(square);
    cosDistanceSquared[index] = Math.cos(square);
    sinBaseAngle[index] = Math.sin(angle);
    cosBaseAngle[index] = Math.cos(angle);
  }

  const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  let reducedMotion = motionQuery.matches;
  let stillPhaseSeconds = 0;
  let width = 1;
  let height = 1;
  let dpr = 1;
  let scale = 1;
  let centerX = 0;
  let centerY = 0;
  let lineWidth = 0.85;
  let anchorRect = null;
  let symbolFrame = null;
  let frameCount = 0;
  let animationFrame = 0;
  let lastPaintAt = -Infinity;

  function continuousPhaseSeconds(now = performance.now()) {
    // Derive phase from the original monotonic epoch rather than summing
    // animation frames. A hidden tab can stop painting without restarting.
    const elapsed = Math.max(0, (now - epoch) / 1000);
    return elapsed % LOOP_SECONDS;
  }

  function currentPhaseSeconds(now = performance.now()) {
    return reducedMotion ? stillPhaseSeconds : continuousPhaseSeconds(now);
  }

  function coefficients(t) {
    return {
      sinT: Math.sin(t), cosT: Math.cos(t),
      sinTripleT: Math.sin(3 * t), cosTripleT: Math.cos(3 * t),
      sinQuarterT: Math.sin(t / 4), cosQuarterT: Math.cos(t / 4)
    };
  }

  function projectPoint(index, phase) {
    const q = constantRadius[index]
      - distanceSquared[index] * (phase.sinT * cosDistance[index]
        - phase.cosT * sinDistance[index])
      + radialWeight[index] * (sinDistanceSquared[index] * phase.cosTripleT
        - cosDistanceSquared[index] * phase.sinTripleT);
    const sinAngle = sinBaseAngle[index] * phase.cosQuarterT
      - cosBaseAngle[index] * phase.sinQuarterT;
    const cosAngle = cosBaseAngle[index] * phase.cosQuarterT
      + sinBaseAngle[index] * phase.sinQuarterT;
    return {x: q * sinAngle + 200, y: q * cosAngle + 200};
  }

  function paint(now = performance.now()) {
    const t = currentPhaseSeconds(now) * PHASE_PERIOD / LOOP_SECONDS;
    const phase = coefficients(t);
    context.clearRect(0, 0, width, height);
    context.save();
    // Match the visible colored symbol, excluding the wordmark below it.
    context.beginPath();
    context.rect(symbolFrame.x, symbolFrame.y, symbolFrame.w, symbolFrame.h);
    context.clip();
    context.strokeStyle = 'rgba(110,118,116,0.36)';
    context.lineWidth = lineWidth;
    context.lineCap = 'round';
    context.lineJoin = 'round';
    context.beginPath();
    // A tiny independent segment with round caps is one point, not a link
    // to the next sample. One stroke batches the full point cloud.
    for (let index = 0; index < POINT_COUNT; index++) {
      const q = constantRadius[index]
        - distanceSquared[index] * (phase.sinT * cosDistance[index]
          - phase.cosT * sinDistance[index])
        + radialWeight[index] * (sinDistanceSquared[index] * phase.cosTripleT
          - cosDistanceSquared[index] * phase.sinTripleT);
      const sinAngle = sinBaseAngle[index] * phase.cosQuarterT
        - cosBaseAngle[index] * phase.sinQuarterT;
      const cosAngle = cosBaseAngle[index] * phase.cosQuarterT
        + sinBaseAngle[index] * phase.sinQuarterT;
      const x = centerX + q * sinAngle * scale;
      const y = centerY + q * cosAngle * scale;
      context.moveTo(x, y);
      context.lineTo(x + 0.01, y);
    }
    context.stroke();
    context.restore();
    frameCount++;
    lastPaintAt = now;
  }

  function resize() {
    const bounds = canvas.getBoundingClientRect();
    width = Math.max(1, bounds.width || window.innerWidth || 1);
    height = Math.max(1, bounds.height || window.innerHeight || 1);
    const header = document.querySelector('body > header');
    const headerHeight = header ? Math.min(
      Math.max(0, header.getBoundingClientRect().height), height * 0.25
    ) : 0;
    const hero = document.querySelector('.brand-art');
    const art = hero ? hero.getBoundingClientRect() : null;
    if (art && art.width > 0 && art.height > 0
        && Number.isFinite(art.left) && Number.isFinite(art.top)) {
      // The hero belongs to the first screen. Its document coordinates stay
      // fixed while the three sections scroll over this persistent canvas.
      anchorRect = {
        x: art.left + (window.scrollX || 0),
        y: art.top + (window.scrollY || 0),
        w: art.width, h: art.height
      };
      // logo.js uses a 1400-square crop of the supplied artwork. The colored
      // symbol's alpha bounds are (120,25)..(1280,1184); the wordmark starts
      // lower down. The formula's full-cycle bounds span about 320 units.
      symbolFrame = {
        x: anchorRect.x + anchorRect.w * 120 / 1400,
        y: anchorRect.y + anchorRect.h * 25 / 1400,
        w: anchorRect.w * 1160 / 1400,
        h: anchorRect.h * 1159 / 1400
      };
      centerX = symbolFrame.x + symbolFrame.w / 2;
      centerY = symbolFrame.y + symbolFrame.h / 2;
      scale = Math.min(symbolFrame.w, symbolFrame.h) / 320;
    } else {
      const side = Math.min(width * 0.9, (height - headerHeight) * 0.9);
      anchorRect = null;
      centerX = width / 2;
      centerY = headerHeight + (height - headerHeight) / 2;
      symbolFrame = {x: centerX - side / 2, y: centerY - side / 2, w: side, h: side};
      scale = side / 400;
    }
    lineWidth = Math.max(0.65, Math.min(2.4, 0.85 * scale));
    dpr = Math.min(window.devicePixelRatio || 1, 1.5,
      Math.sqrt(MAX_BACKING_PIXELS / (width * height)));
    canvas.width = Math.max(1, Math.round(width * dpr));
    canvas.height = Math.max(1, Math.round(height * dpr));
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!document.hidden) paint();
  }

  function tick(now) {
    animationFrame = 0;
    if (document.hidden || reducedMotion) return;
    const frameInterval = 1000 / (width < 700 ? 24 : 30);
    if (now - lastPaintAt >= frameInterval) paint(now);
    animationFrame = window.requestAnimationFrame(tick);
  }

  function syncAnimation() {
    if (animationFrame) window.cancelAnimationFrame(animationFrame);
    animationFrame = 0;
    if (!document.hidden) paint();
    if (!document.hidden && !reducedMotion) {
      animationFrame = window.requestAnimationFrame(tick);
    }
  }

  function motionChanged(event) {
    if (event.matches === reducedMotion) return;
    if (event.matches) stillPhaseSeconds = continuousPhaseSeconds();
    reducedMotion = event.matches;
    syncAnimation();
  }

  // Publish the guard before registering listeners, so loading this asset
  // again can never add another timer or reset the shared animation clock.
  window.__pldpSeaSlugBackground = {
    version: 2,
    getState() {
      const phaseSeconds = currentPhaseSeconds();
      return {
        phaseSeconds, t: phaseSeconds * PHASE_PERIOD / LOOP_SECONDS,
        loopSeconds: LOOP_SECONDS, phasePeriod: PHASE_PERIOD,
        samplingDivisor: SAMPLING_DIVISOR, pointCount: POINT_COUNT,
        canvasCount: document.querySelectorAll('#pldp-sea-slug-background').length,
        frameCount, width, height, lineWidth, dpr,
        anchorRect: anchorRect ? {...anchorRect} : null,
        symbolFrame: symbolFrame ? {...symbolFrame} : null,
        centerX, centerY, scale,
        running: Boolean(animationFrame) && !document.hidden && !reducedMotion,
        reducedMotion
      };
    },
    samplePoint(index, t) {
      if (!Number.isInteger(index) || index < 0 || index >= POINT_COUNT) {
        throw new RangeError('Sample index must be an integer from 0 to 19999.');
      }
      const phase = t === undefined
        ? currentPhaseSeconds() * PHASE_PERIOD / LOOP_SECONDS : t;
      if (!Number.isFinite(phase)) throw new TypeError('Sample phase must be finite.');
      return projectPoint(index, coefficients(phase));
    }
  };

  window.addEventListener('resize', resize, {passive: true});
  const hero = document.querySelector('.brand-art');
  if (hero && typeof ResizeObserver !== 'undefined') {
    const observer = new ResizeObserver(resize);
    observer.observe(hero);
  }
  document.addEventListener('visibilitychange', syncAnimation);
  if (motionQuery.addEventListener) motionQuery.addEventListener('change', motionChanged);
  else motionQuery.addListener(motionChanged);
  resize();
  syncAnimation();
})();
