// =========================================================================
// ViFi — Slides part 2: commercial case + comparison + roadmap
// =========================================================================

// ---------- 08: Unit economics — $44 vs $3-5k ----------
function Slide08UnitEconomics() {
  return (
    <SlideFrame>
      <MicroLabel>03 · Unit economics</MicroLabel>
      <SlideTitle style={{marginTop: SPACING.titleGap, maxWidth: 1500}}>
        A two-order-of-magnitude cost gap.
      </SlideTitle>

      <div style={{marginTop: 56, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 56, flex: 1, alignItems: 'stretch'}}>
        <div style={{
          background: C.paper2, border: `1px solid ${C.paper3}`,
          borderRadius: 4, padding: '56px 56px 64px',
          display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
        }}>
          <MicroLabel>Traditional bedside monitor</MicroLabel>
          <div>
            <div style={{
              fontFamily: FONT_SANS, fontSize: 200, fontWeight: 500,
              lineHeight: 0.92, letterSpacing: '-0.035em', color: C.fg1,
              fontVariantNumeric: 'tabular-nums',
            }}>
              $3–5k
            </div>
            <div style={{marginTop: 16, fontSize: 32, color: C.fg2, letterSpacing: '-0.005em'}}>per bed, hardware only</div>
          </div>
          <div style={{fontSize: 28, color: C.fg2, lineHeight: 1.5, maxWidth: 540}}>
            Deployed in the ~10% of hospital beds that are ICU. Every other bed gets manual rounds.
          </div>
        </div>

        <div style={{
          background: C.ink, color: C.onDark1, border: `1px solid ${C.ink3}`,
          borderRadius: 4, padding: '56px 56px 64px',
          display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
        }}>
          <MicroLabel dark style={{color: C.signal}}>ViFi</MicroLabel>
          <div>
            <div style={{
              fontFamily: FONT_SANS, fontSize: 280, fontWeight: 500,
              lineHeight: 0.88, letterSpacing: '-0.045em', color: C.signal,
              fontVariantNumeric: 'tabular-nums',
            }}>
              $44
            </div>
            <div style={{marginTop: 16, fontSize: 32, color: C.onDark2, letterSpacing: '-0.005em'}}>per room, full BOM</div>
          </div>
          <div style={{fontSize: 24, color: C.onDark2, lineHeight: 1.5, maxWidth: 540}}>
            2× ESP32-S3-DevKitC nodes ($30) + 2× dual-band antennas ($8) + 2× IPEX → RP-SMA pigtails ($6). Commodity silicon shipping a billion units a year.
          </div>
        </div>
      </div>
      <Footer label="Unit economics" pageNum="08 / 16"/>
    </SlideFrame>
  );
}

// ---------- 09: Comparison table ----------
function Slide09Comparison() {
  const cellPad = '22px 28px';
  return (
    <SlideFrame>
      <MicroLabel>03 · Why it's different</MicroLabel>
      <SlideTitle style={{marginTop: SPACING.titleGap}}>Why it's different.</SlideTitle>

      <div style={{marginTop: 48, flex: 1}}>
        <table style={{width: '100%', borderCollapse: 'collapse', fontVariantNumeric: 'tabular-nums'}}>
          <thead>
            <tr>
              <th style={{...thStyle, width: 280}}></th>
              <th style={{...thStyle, fontSize: 24}}>Traditional monitors</th>
              <th style={{...thStyle, fontSize: 24, color: C.fg1}}>ViFi</th>
            </tr>
          </thead>
          <tbody>
            {[
              ['Contact',       'Wires, adhesives, compliance',         'WiFi signals, no contact'],
              ['Cost per bed',  '$3,000 – $5,000',                      '$44 (full room BOM)'],
              ['Deployment',    'ICU only (~10% of beds)',              'Every bed'],
              ['Coverage',      'Rounds every 4–8 hours',               'Continuous, 24/7'],
              ['Vitals',        'One dedicated monitor per vital',      'Six capabilities, one device'],
              ['HR accuracy',   'Reference grade',                      '4.15 bpm MAE today, target <3'],
            ].map((r, i) => (
              <tr key={i}>
                <td style={{padding: cellPad, borderBottom: `1px solid ${C.paper3}`,
                  fontSize: 24, letterSpacing: '0.1em', textTransform: 'uppercase', color: C.fg3, fontWeight: 500}}>{r[0]}</td>
                <td style={{padding: cellPad, borderBottom: `1px solid ${C.paper3}`, fontSize: 26, color: C.fg2, lineHeight: 1.35}}>{r[1]}</td>
                <td style={{padding: cellPad, borderBottom: `1px solid ${C.paper3}`, fontSize: 26, color: C.fg1, fontWeight: 500, lineHeight: 1.35}}>{r[2]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Footer label="Comparison" pageNum="09 / 16"/>
    </SlideFrame>
  );
}
const thStyle = {
  textAlign: 'left', padding: '20px 28px',
  fontSize: 24, fontWeight: 500, letterSpacing: '0.1em', textTransform: 'uppercase',
  color: C.fg3, borderBottom: `2px solid ${C.paper3}`,
};

// ---------- 10: Why now — prior art ----------
function Slide10WhyNow() {
  const papers = [
    {venue: 'INFOCOM 2017',  name: 'PhaseBeat',     body: 'HR + RR from WiFi CSI on Intel 5300 NICs ($500/node). 1.5 bpm MAE — our $44 hardware: 4.15 bpm.'},
    {venue: 'UbiComp 2018',  name: 'FullBreathe',   body: 'Respiratory monitoring through walls, fine-grained enough for apnea detection.'},
    {venue: '2020',          name: 'ResBeat',       body: 'Robust heart-rate extraction across body positions and sleep states.'},
    {venue: 'MobiCom 2016',  name: 'WiGait / WiFall', body: 'Gait parameters and fall detection from the same signal class.'},
  ];
  return (
    <SlideFrame dark>
      <MicroLabel dark>04 · Why now</MicroLabel>
      <SlideTitle dark style={{marginTop: SPACING.titleGap, maxWidth: 1500}}>
        A decade of academic work. No one productized it.
      </SlideTitle>
      <div style={{marginTop: 36, fontSize: 32, lineHeight: 1.45, color: C.onDark2, maxWidth: 1500}}>
        WiFi CSI vitals extraction is a solved research problem. Our contribution is not novel signal processing — it's clinical packaging on $44 of commodity silicon.
      </div>

      <div style={{marginTop: 40, display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 22, flex: 1}}>
        {papers.map(p => (
          <div key={p.name} style={{
            background: C.ink2, border: `1px solid ${C.ink3}`,
            borderRadius: 4, padding: '22px 28px',
            display: 'flex', flexDirection: 'column', gap: 8,
          }}>
            <div style={{fontFamily: FONT_MONO, fontSize: 20, color: C.onDark3, letterSpacing: '0.06em'}}>{p.venue}</div>
            <div style={{fontSize: 28, fontWeight: 600, color: C.onDark1, letterSpacing: '-0.005em'}}>{p.name}</div>
            <div style={{fontSize: 22, lineHeight: 1.4, color: C.onDark2}}>{p.body}</div>
          </div>
        ))}
      </div>
      <Footer label="Why now" pageNum="10 / 16" dark/>
    </SlideFrame>
  );
}

// ---------- 11: Roadmap ----------
function Slide11Roadmap() {
  const markers = [
    {date: 'April 2026 · today', body: '4.15 bpm cross-session HR MAE on real ESP32-S3 hardware. 41 tests passing.', now: true},
    {date: 'May 2026',           body: 'Multi-subject HR validation (10+ subjects). Multi-room bias study.'},
    {date: 'June 2026',          body: 'Respiratory-rate paired captures with Vernier respiration belt.'},
    {date: 'Months 2–4',         body: 'Apnea + transient-event logger. Phase-domain features.'},
    {date: 'Months 4–8',         body: 'Gait, falls, 4-receiver multi-patient array.'},
    {date: 'Months 9–12',        body: 'First hospital pilot, 5–10 beds, wellness-grade, pre-FDA.'},
    {date: 'Months 12–18',       body: 'FDA 510(k) Class II filing for vitals monitoring (~$300K all-in).'},
  ];
  return (
    <SlideFrame>
      <MicroLabel>05 · Where we are</MicroLabel>
      <SlideTitle style={{marginTop: SPACING.titleGap}}>Where we are.</SlideTitle>

      <div style={{marginTop: 44, position: 'relative', paddingLeft: 64, flex: 1}}>
        <div style={{position: 'absolute', left: 14, top: 16, bottom: 16, width: 2, background: C.paper3}}/>
        {markers.map((m, i) => (
          <div key={i} style={{position: 'relative', marginBottom: i === markers.length - 1 ? 0 : 22, display: 'grid', gridTemplateColumns: '260px 1fr', gap: 36, alignItems: 'baseline'}}>
            <span style={{
              position: 'absolute', left: -58, top: 10, borderRadius: 999,
              width: m.now ? 24 : 18, height: m.now ? 24 : 18,
              background: m.now ? C.signal : C.paper,
              border: m.now ? `5px solid ${C.paper}` : `2px solid ${C.fg3}`,
              boxShadow: m.now ? `0 0 0 2px ${C.signal}` : 'none',
              boxSizing: 'border-box',
            }}/>
            <div style={{fontFamily: FONT_MONO, fontSize: 22, color: m.now ? C.signalInk : C.fg3, letterSpacing: '0.04em', fontWeight: 500, paddingTop: 4}}>{m.date}</div>
            <div style={{fontSize: 28, fontWeight: 500, lineHeight: 1.3, letterSpacing: '-0.005em', maxWidth: 1200}}>{m.body}</div>
          </div>
        ))}
      </div>

      <div style={{marginTop: 20, fontSize: 24, color: C.fg2, lineHeight: 1.5, maxWidth: 1500, fontStyle: 'italic'}}>
        Technical risk — "does this work on commodity hardware at all" — is retired. Remaining risk is multi-subject generalization, regulatory, and hospital sales. That's what funding addresses.
      </div>
      <Footer label="Where we are" pageNum="11 / 16"/>
    </SlideFrame>
  );
}

window.Slide08UnitEconomics = Slide08UnitEconomics;
window.Slide09Comparison = Slide09Comparison;
window.Slide10WhyNow = Slide10WhyNow;
window.Slide11Roadmap = Slide11Roadmap;
