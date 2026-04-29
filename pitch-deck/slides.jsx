// =========================================================================
// ViFi — YC Pitch Deck
// 1920x1080, projection-scale type, clinical/editorial voice.
// =========================================================================

const TYPE_SCALE = {
  display: 116,   // hero slide monster
  title:   76,    // slide titles
  subtitle:56,    // section openers
  bignum:  220,   // stat slides
  lead:    40,    // lead paragraph on content slides
  body:    32,    // standard body
  serif:   36,    // origin / problem serif paragraphs
  small:   26,    // captions, meta
  label:   24,    // uppercase micro-labels
};

const SPACING = {
  paddingTop:    84,
  paddingBottom: 96,
  paddingX:      120,
  titleGap:      44,
  itemGap:       28,
  sectionGap:    64,
};

// ---------- Color tokens (from ViFi design system) ----------
const C = {
  ink:       '#0A1628',
  ink2:      '#13233C',
  ink3:      '#1E3354',
  paper:     '#F8F8F5',
  paper2:    '#F1F1EC',
  paper3:    '#E6E6DF',
  fg1:       '#1A1A1A',
  fg2:       '#4A5568',
  fg3:       '#7A8699',
  onDark1:   '#F8F8F5',
  onDark2:   '#A8B4C8',
  onDark3:   '#6B7A92',
  signal:    '#00B87C',
  signalInk: '#008A5C',
  signalBg:  '#E6F7F0',
  amber:     '#B88A00',
};

const FONT_SANS  = "'Inter', system-ui, -apple-system, sans-serif";
const FONT_SERIF = "'IBM Plex Serif', Georgia, serif";
const FONT_MONO  = "'IBM Plex Mono', ui-monospace, Menlo, monospace";

// =========================================================================
// Primitives
// =========================================================================

function SlideFrame({dark, children, noPad, style}) {
  return (
    <div style={{
      width: '100%', height: '100%',
      background: dark ? C.ink : C.paper,
      color: dark ? C.onDark1 : C.fg1,
      fontFamily: FONT_SANS,
      padding: noPad ? 0 : `${SPACING.paddingTop}px ${SPACING.paddingX}px ${SPACING.paddingBottom}px`,
      display: 'flex', flexDirection: 'column',
      boxSizing: 'border-box',
      position: 'relative',
      ...style,
    }}>
      {children}
    </div>
  );
}

function MicroLabel({children, dark, color, style}) {
  return (
    <div style={{
      fontFamily: FONT_SANS,
      fontSize: TYPE_SCALE.label,
      fontWeight: 500,
      letterSpacing: '0.14em',
      textTransform: 'uppercase',
      color: color || (dark ? C.onDark3 : C.fg3),
      ...style,
    }}>{children}</div>
  );
}

function SlideTitle({children, dark, size, style}) {
  return (
    <h2 style={{
      fontFamily: FONT_SANS,
      fontSize: size || TYPE_SCALE.title,
      fontWeight: 500,
      lineHeight: 1.08,
      letterSpacing: '-0.015em',
      color: dark ? C.onDark1 : C.fg1,
      margin: 0,
      maxWidth: 1500,
      textWrap: 'balance',
      ...style,
    }}>{children}</h2>
  );
}

function Footer({label, pageNum, dark}) {
  return (
    <div style={{
      position: 'absolute',
      left: SPACING.paddingX, right: SPACING.paddingX,
      bottom: 44,
      display: 'flex', justifyContent: 'space-between', alignItems: 'center',
      fontFamily: FONT_SANS, fontSize: 24, color: dark ? C.onDark3 : C.fg3,
      letterSpacing: '0.08em', textTransform: 'uppercase', fontWeight: 500,
    }}>
      <span>ViFi · YC S26</span>
      <span>{label}</span>
      <span style={{fontFeatureSettings: "'tnum'"}}>{pageNum}</span>
    </div>
  );
}

function StatusChip({status, big}) {
  const map = {
    shipped: {bg: C.signalBg, fg: C.signalInk, dot: C.signal, label: 'Shipped'},
    dev:     {bg: '#FAF3DD', fg: '#8A6500', dot: C.amber,   label: 'In dev'},
    planned: {bg: '#EDEFF3', fg: C.fg2,      dot: C.fg3,     label: 'Planned'},
  }[status];
  const fs = big ? 24 : 24;
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: big ? 12 : 10,
      padding: big ? '8px 18px' : '7px 16px', borderRadius: 999,
      background: map.bg, color: map.fg,
      fontSize: fs, fontWeight: 500, letterSpacing: '0.1em',
      textTransform: 'uppercase', fontFamily: FONT_SANS, whiteSpace: 'nowrap',
    }}>
      <span style={{width: big ? 10 : 8, height: big ? 10 : 8, borderRadius: 999, background: map.dot}}/>
      {map.label}
    </span>
  );
}

window.TYPE_SCALE = TYPE_SCALE;
window.SPACING = SPACING;
window.C = C;
window.FONT_SANS = FONT_SANS;
window.FONT_SERIF = FONT_SERIF;
window.FONT_MONO = FONT_MONO;
window.SlideFrame = SlideFrame;
window.SlideTitle = SlideTitle;
window.MicroLabel = MicroLabel;
window.Footer = Footer;
window.StatusChip = StatusChip;
