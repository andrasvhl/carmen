window.__audit = function () {
  const W = document.documentElement.clientWidth, out = { w: W, hscroll: document.documentElement.scrollWidth > W + 1, offscreen: [], clipped: [], small: [], overlap: [], contrast: [] };
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity > 0.05; };
  const label = el => (el.innerText || el.getAttribute('aria-label') || el.tagName).trim().replace(/\s+/g, ' ').slice(0, 40);
  const all = [...document.querySelectorAll('body *')].filter(vis);
  for (const el of all) {
    const r = el.getBoundingClientRect();
    if (r.right > W + 2 || r.left < -2) { if (!el.closest('[style*="overflow"],.scroll,.hscroll')) out.offscreen.push(label(el) + ` [${Math.round(r.left)},${Math.round(r.right)}]`); }
    const s = getComputedStyle(el);
    if (el.children.length === 0 && el.innerText && el.scrollWidth > el.clientWidth + 2 && s.textOverflow !== 'ellipsis' && s.overflow !== 'visible' && s.overflowX !== 'auto' && s.overflowX !== 'scroll') out.clipped.push(label(el));
  }
  const btns = [...document.querySelectorAll('button,a,[role=button],input,select,textarea')].filter(vis);
  for (const b of btns) { const r = b.getBoundingClientRect(); if ((r.height < 28 || r.width < 28) && !b.closest('p,li,.md')) out.small.push(label(b) + ` ${Math.round(r.width)}x${Math.round(r.height)}`); }
  for (let i = 0; i < btns.length; i++) for (let j = i + 1; j < btns.length; j++) {
    const a = btns[i].getBoundingClientRect(), c = btns[j].getBoundingClientRect();
    if (btns[i].contains(btns[j]) || btns[j].contains(btns[i])) continue;
    const ox = Math.min(a.right, c.right) - Math.max(a.left, c.left), oy = Math.min(a.bottom, c.bottom) - Math.max(a.top, c.top);
    if (ox > 4 && oy > 4) out.overlap.push(label(btns[i]) + ' <> ' + label(btns[j]));
  }
  const lum = c => { const m = c.match(/[\d.]+/g); if (!m) return null; const [r, g, b] = m.slice(0, 3).map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }); return { L: .2126 * r + .7152 * g + .0722 * b, a: m[3] === undefined ? 1 : +m[3] }; };
  const bgOf = el => { while (el) { const b = lum(getComputedStyle(el).backgroundColor); if (b && b.a > 0.5) return b.L; el = el.parentElement; } return lum(getComputedStyle(document.body).backgroundColor).L; };
  for (const el of all) {
    if (![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue;
    const f = lum(getComputedStyle(el).color); if (!f) continue; const bg = bgOf(el);
    const cr = (Math.max(f.L, bg) + .05) / (Math.min(f.L, bg) + .05);
    if (cr < 3 && f.a > 0.3) out.contrast.push(label(el) + ' ' + cr.toFixed(1));
  }
  for (const k of ['offscreen', 'clipped', 'small', 'overlap', 'contrast']) out[k] = [...new Set(out[k])].slice(0, 15);
  return out;
};
'audit loaded';
