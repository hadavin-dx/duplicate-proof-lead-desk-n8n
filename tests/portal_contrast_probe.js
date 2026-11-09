(root) => {
  const parse = (c) => { const m = c.match(/[\d.]+/g).map(Number); return { r: m[0], g: m[1], b: m[2], a: m.length > 3 ? m[3] : 1 }; };
  const over = (f, b, a) => ({ r: f.r * a + b.r * (1 - a), g: f.g * a + b.g * (1 - a), b: f.b * a + b.b * (1 - a), a: 1 });
  const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
  const out = [];
  const scope = root ? document.querySelector(root) : document.body;
  // the text a control shows is not a text node: a select shows its chosen option, an input its value or its placeholder
  const items = [];
  const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
  for (let t = walker.nextNode(); t; t = walker.nextNode()) if (t.nodeValue.trim() && !t.parentElement.closest('select')) items.push([t.parentElement, t.nodeValue.trim()]);
  scope.querySelectorAll('select').forEach((e) => items.push([e, e.selectedOptions.length ? e.selectedOptions[0].textContent.trim() : '']));
  scope.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea').forEach((e) => items.push([e, (e.value || e.placeholder || '').trim()]));
  for (const [el, text] of items) {
    if (!text) continue;
    const cs = getComputedStyle(el);
    const box = el.getBoundingClientRect();
    if (cs.visibility === 'hidden' || cs.display === 'none' || box.width === 0 || box.height === 0) continue;
    if (el.closest('.sr-only, [aria-hidden=true], script, style')) continue;
    let alpha = 1; for (let n = el; n; n = n.parentElement) alpha *= parseFloat(getComputedStyle(n).opacity);
    if (cs.webkitTextFillColor === 'rgba(0, 0, 0, 0)') continue; // gradient-filled heading text: no single colour to measure
    const layers = []; let gradient = false;
    for (let n = el; n; n = n.parentElement) { const st = getComputedStyle(n); if (st.backgroundImage !== 'none') gradient = true;
      const c = parse(st.backgroundColor); if (c.a > 0) { layers.push(c); if (c.a === 1) break; } }
    if (gradient) continue; // text on a gradient (the primary button): measured by its two end colours in the next test
    let bg = { r: 255, g: 255, b: 255, a: 1 }; for (let i = layers.length - 1; i >= 0; i--) bg = over(layers[i], bg, layers[i].a);
    const fg0 = parse(cs.color); const fg = over(fg0, bg, fg0.a * alpha);
    const l1 = lum(fg), l2 = lum(bg); const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    out.push({ text: text.slice(0, 40), cls: el.className && el.className.baseVal === undefined ? el.className : '', ratio: Math.round(ratio * 100) / 100 });
  }
  return out; }
