/* Appearance v1 for stdlib apps. Same-origin external script: no CSP exception.
 * Token derivation follows wizard-ux/core/appearance-host.js; no app permissions. */
(() => {
  'use strict';
  const root = document.documentElement, origin = location.origin;
  const keys = ['canvas', 'base', 'chrome', 'overlay', 'surface', 'surface-raised', 'text', 'text-secondary', 'text-muted', 'border', 'accent', 'accent-soft', 'on-accent', 'reader-surface'];
  const color = v => typeof v === 'string' && v.length > 0 && v.length < 96 && !/[;{}]|url\(/i.test(v) && CSS.supports('color', v);
  let revision = -1, transported = false;
  function apply(a) {
    for (const k of keys) root.style.setProperty('--wz-' + k, a.tokens[k]);
    root.style.colorScheme = a.colorScheme;
    root.dataset.skin = a.skin;
    root.dataset.colorScheme = a.colorScheme;
    root.dataset.presentation = a.presentation;
  }
  if (parent === window) return;
  addEventListener('message', e => {
    const a = e.data?.appearance;
    if (e.source !== parent || e.origin !== origin || e.data?.type !== 'wizard:appearance' ||
        !a || a.version !== 1 || !Number.isSafeInteger(a.revision) || a.revision < 0 || a.revision < revision ||
        !/^[a-z0-9-]{1,40}$/.test(a.skin || '') || !['light', 'dark'].includes(a.colorScheme) ||
        !['window', 'immersive', 'standalone'].includes(a.presentation) || !a.tokens || !keys.every(k => color(a.tokens[k]))) return;
    apply(a); revision = a.revision; transported = true;
    root.dataset.appearanceSource = 'transport';
    parent.postMessage({type: 'wizard:appearance:ack', version: 1, revision}, origin);
  });
  // Bound/review windows may lack a registered Appearance host. Until transport
  // arrives, use the parent's skin base for legacy frame-background sampling.
  try {
    if (parent.location.origin === origin) {
      const host = parent.document.documentElement;
      const fallback = () => {
        const s = parent.getComputedStyle(host);
        const font = s.getPropertyValue('--font-ui').trim();
        if (font && font.length < 256 && !/[;{}]|url\(/i.test(font)) root.style.setProperty('--wz-font-ui', font);
        if (transported) return;
        const read = (k, f) => { const v = s.getPropertyValue('--' + k).trim(); return color(v) ? v : f; };
        const light = /\blight\b/.test(s.colorScheme) && !/\bdark\b/.test(s.colorScheme);
        const base = read('bg', light ? '#f6f3ec' : '#05030c');
        const ink = read('ink', light ? '#1d1b17' : '#f3efff'), accent = read('violet-2', '#b99bff');
        apply({skin: /^[a-z0-9-]{1,40}$/.test(host.dataset.skin || '') ? host.dataset.skin : 'nebula',
          colorScheme: light ? 'light' : 'dark', presentation: 'window', tokens: {
            canvas: base, base, chrome: read('sk-win', 'rgba(12,8,26,.82)'), overlay: read('glass-2', 'rgba(28,19,58,.72)'),
            surface: `color-mix(in srgb, ${ink} 6%, transparent)`, 'surface-raised': `color-mix(in srgb, ${ink} 10%, transparent)`,
            text: ink, 'text-secondary': read('ink-2', '#b9b0d8'), 'text-muted': read('ink-3', '#7d74a3'),
            border: read('line-2', 'rgba(205,190,255,.24)'), accent, 'accent-soft': `color-mix(in srgb, ${accent} 16%, transparent)`,
            'on-accent': light ? '#fffdf8' : base, 'reader-surface': base
          }});
        root.dataset.appearanceSource = 'parent-fallback';
      };
      fallback();
      new MutationObserver(fallback).observe(host, {attributes: true, attributeFilter: ['style', 'class', 'data-skin']});
    }
  } catch (_) { /* Cross-origin hosts retain standalone defaults. */ }
  parent.postMessage({type: 'wizard:appearance:ready', version: 1}, origin);
})();
