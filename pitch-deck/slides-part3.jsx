// =========================================================================
// ViFi — Slides part 3: market, traction, origin, team, close
// =========================================================================

// ---------- 12: Market sizing (the honest version) ----------
function Slide12Market() {
  return (
    <SlideFrame>
      <MicroLabel>06 · Market</MicroLabel>
      <SlideTitle style={{marginTop: SPACING.titleGap, maxWidth: 1500}}>
        Every hospital bed is a potential deployment.
      </SlideTitle>

      <div style={{marginTop: 72, display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 48, flex: 1}}>
        {[
          {num: '920,000', unit: 'U.S. hospital beds', body: 'AHA 2024. Roughly 10% are ICU — the only beds that get continuous monitoring today.'},
          {num: '18.6M',   unit: 'hospital beds worldwide', body: 'WHO, most in countries where $3k monitors are economically impossible.'},
          {num: '$44',     unit: 'per room, our hardware', body: 'Two orders of magnitude cheaper. Turns non-ICU beds into a viable market.'},
        ].map((m, i) => (
          <div key={i} style={{
            display: 'flex', flexDirection: 'column', gap: 24,
            paddingTop: 32, borderTop: `2px solid ${C.ink}`,
          }}>
            <div style={{
              fontFamily: FONT_SANS, fontSize: 124, fontWeight: 500,
              lineHeight: 0.92, letterSpacing: '-0.035em',
              fontVariantNumeric: 'tabular-nums',
            }}>{m.num}</div>
            <MicroLabel>{m.unit}</MicroLabel>
            <div style={{fontSize: 26, color: C.fg2, lineHeight: 1.5, maxWidth: 'none'}}>{m.body}</div>
          </div>
        ))}
      </div>
      <div style={{marginTop: 32, fontSize: 26, color: C.fg3, lineHeight: 1.5, maxWidth: 1500, fontStyle: 'italic'}}>
        We're not forecasting TAM capture. We're saying: at $44/room, the market stops being the ICU and starts being every bed.
      </div>
      <Footer label="Market" pageNum="12 / 16"/>
    </SlideFrame>
  );
}

// ---------- 13: Traction — what's real today ----------
function Slide13Traction() {
  const rows = [
    {label: 'Real-hardware HR', status: 'shipped', body: '4.15 bpm cross-session MAE vs Polar H10 (leave-one-session-out, 4 paired captures)'},
    {label: 'DSP pipeline',     status: 'shipped', body: 'Bandpass + FFT + XGBoost. 41 tests passing on main.'},
    {label: 'Capture rig',      status: 'shipped', body: 'Hands-free 2-min paired capture; CSI + Polar H10 BLE simultaneously'},
    {label: 'Presence detect',  status: 'shipped', body: 'Variance-threshold occupancy live at /predict/presence'},
    {label: 'Multi-subject',    status: 'dev',     body: 'May 2026: 10+ subjects, target cross-subject MAE <3 bpm'},
    {label: 'FDA 510(k)',       status: 'planned', body: 'Wellness-grade pilot first (months 9–12); 510(k) filing months 12–18'},
  ];
  return (
    <SlideFrame>
      <MicroLabel>07 · Traction</MicroLabel>
      <SlideTitle style={{marginTop: SPACING.titleGap}}>What's real today.</SlideTitle>

      <div style={{marginTop: 40, flex: 1, display: 'flex', flexDirection: 'column'}}>
        {rows.map((r, i) => (
          <div key={i} style={{
            display: 'grid', gridTemplateColumns: '280px 200px 1fr',
            alignItems: 'center', gap: 32,
            padding: '22px 0',
            borderBottom: `1px solid ${C.paper3}`,
          }}>
            <div style={{fontSize: 30, fontWeight: 600, letterSpacing: '-0.005em'}}>{r.label}</div>
            <div><StatusChip status={r.status} big/></div>
            <div style={{fontSize: 22, color: C.fg2, lineHeight: 1.45}}>{r.body}</div>
          </div>
        ))}
      </div>
      <div style={{marginTop: 18, fontSize: 22, color: C.fg3, fontFamily: FONT_MONO, letterSpacing: '0.02em'}}>
        github.com/Zpopowitz/vifi-ml · 41 tests passing · MIT licensed
      </div>
      <Footer label="Traction" pageNum="13 / 16"/>
    </SlideFrame>
  );
}

// ---------- 14: Origin (serif treatment) ----------
function Slide14Origin() {
  return (
    <SlideFrame>
      <MicroLabel>08 · Why we started this</MicroLabel>
      <div style={{marginTop: SPACING.titleGap, display: 'grid', gridTemplateColumns: '1fr 1.4fr', gap: 100, flex: 1, alignItems: 'stretch'}}>
        <div style={{display: 'flex', flexDirection: 'column', justifyContent: 'space-between'}}>
          <div>
            <SlideTitle size={84}>
              Why we<br/>started this.
            </SlideTitle>
            <div style={{marginTop: 44, fontSize: 30, color: C.fg2, lineHeight: 1.5, maxWidth: 480}}>
              The worst part wasn't the misdiagnosis. It was that no one was watching between rounds.
            </div>
          </div>
          <div style={{
            paddingTop: 28, borderTop: `1px solid ${C.paper3}`,
            fontSize: 26, color: C.fg3, letterSpacing: '0.04em',
          }}>
            — Zach Popowitz, Founder
          </div>
        </div>

        <div style={{fontFamily: FONT_SERIF, fontSize: 38, lineHeight: 1.5, color: C.fg1, alignSelf: 'center'}}>
          <p style={{margin: '0 0 32px', maxWidth: 'none'}}>
            My mom's first pain was November 13. By November 22 she had been seen four times and told it was a UTI. It wasn't.
          </p>
          <p style={{margin: '0 0 32px', maxWidth: 'none'}}>
            The real diagnosis was Staphylococcus bacteremia with spinal and intra-abdominal abscesses. She spent 5 nights in the hospital and didn't come home fully until December 14.
          </p>
          <p style={{margin: 0, fontStyle: 'italic', color: C.ink, maxWidth: 'none'}}>
            Her vitals spiked between rounds. The chart said "stable." ViFi is the system I wish had been in her room.
          </p>
        </div>
      </div>
      <Footer label="Origin" pageNum="14 / 16"/>
    </SlideFrame>
  );
}

// ---------- 15: Team ----------
function Slide15Team() {
  return (
    <SlideFrame>
      <MicroLabel>09 · Team</MicroLabel>
      <SlideTitle style={{marginTop: SPACING.titleGap}}>Team.</SlideTitle>

      <div style={{marginTop: 56, display: 'grid', gridTemplateColumns: '1.1fr 1fr', gap: 100, flex: 1, alignItems: 'start'}}>
        <div>
          <div style={{display: 'flex', alignItems: 'center', gap: 28, marginBottom: 28}}>
            <div style={{
              width: 100, height: 100, borderRadius: 999, background: C.ink,
              color: C.signal, display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontFamily: FONT_SANS, fontSize: 40, fontWeight: 500,
            }}>ZP</div>
            <div>
              <div style={{fontSize: 40, fontWeight: 600, lineHeight: 1.15, letterSpacing: '-0.01em'}}>Zach Popowitz</div>
              <div style={{marginTop: 6, fontSize: 26, color: C.fg2}}>Founder · CEO · Sole engineer (today)</div>
            </div>
          </div>
          <div style={{fontSize: 24, lineHeight: 1.55, color: C.fg1, maxWidth: 640}}>
            Built the full ML pipeline, FastAPI layer, and clinician dashboard from scratch. Previously ML engineer in production clinical-NLP systems. Personal motivation: my mom's misdiagnosis.
          </div>
        </div>

        <div style={{
          background: C.paper2, border: `1px solid ${C.paper3}`,
          borderRadius: 4, padding: '36px 40px',
        }}>
          <MicroLabel>Hiring now</MicroLabel>
          <div style={{marginTop: 24, display: 'flex', flexDirection: 'column', gap: 20}}>
            {[
              {title: 'RF / signal-processing engineer', body: 'Co-founder seat. WiFi CSI, beamforming, real-hardware tuning.'},
              {title: 'Clinical partnerships lead',       body: 'Academic medical center relationships, pilot-site ops.'},
              {title: 'Regulatory advisor',               body: '510(k) path, predicate identification, QMS setup.'},
            ].map((h, i) => (
              <div key={i} style={{paddingBottom: 18, borderBottom: i < 2 ? `1px solid ${C.paper3}` : 'none'}}>
                <div style={{fontSize: 26, fontWeight: 600, letterSpacing: '-0.005em'}}>{h.title}</div>
                <div style={{marginTop: 6, fontSize: 24, color: C.fg2, lineHeight: 1.45, maxWidth: 'none'}}>{h.body}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
      <Footer label="Team" pageNum="15 / 16"/>
    </SlideFrame>
  );
}

// ---------- 16: Close ----------
function Slide16Close() {
  return (
    <SlideFrame dark noPad>
      <div style={{
        padding: `100px 120px 120px`,
        height: '100%', display: 'flex', flexDirection: 'column', justifyContent: 'center',
        position: 'relative', zIndex: 1, boxSizing: 'border-box',
      }}>
        <img src="assets/logo/vifi-logo-dark.png" alt="ViFi" style={{height: 84, width: 'auto', marginBottom: 44}}/>
        <h2 style={{
          fontFamily: FONT_SANS, fontSize: 140, fontWeight: 500,
          lineHeight: 0.96, letterSpacing: '-0.035em', color: C.onDark1,
          margin: 0, maxWidth: 1700, textWrap: 'balance',
        }}>
          Contactless hospital vitals<br/>from WiFi signals.
        </h2>
        <div style={{marginTop: 40, fontSize: 40, lineHeight: 1.4, color: C.onDark2, maxWidth: 1600}}>
          <span style={{color: C.signal, fontWeight: 500}}>4.15 bpm MAE</span> today, on $44 of hardware.
          Every bed. Every vital the room can tell us.
        </div>

        <div style={{
          marginTop: 60, paddingTop: 32, borderTop: `1px solid ${C.ink3}`,
          display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 48, maxWidth: 1500,
        }}>
          {[
            {label: 'Founder', value: 'Zach Popowitz'},
            {label: 'Email', value: 'zach@vifi.health'},
            {label: 'Code', value: 'github.com/Zpopowitz/vifi-ml'},
          ].map((c, i) => (
            <div key={i}>
              <MicroLabel dark style={{color: C.onDark3, marginBottom: 10}}>{c.label}</MicroLabel>
              <div style={{fontSize: 28, fontWeight: 500, color: C.onDark1, fontFamily: i === 0 ? FONT_SANS : FONT_MONO, letterSpacing: i === 0 ? '-0.005em' : '0.01em'}}>
                {c.value}
              </div>
            </div>
          ))}
        </div>
      </div>
      <div style={{
        position: 'absolute', left: SPACING.paddingX, right: SPACING.paddingX, bottom: 52,
        display: 'flex', justifyContent: 'space-between',
        fontSize: 24, color: C.onDark3, letterSpacing: '0.08em', textTransform: 'uppercase', fontWeight: 500,
      }}>
        <span>Pre-commercial. Not currently FDA-cleared.</span>
        <span>16 / 16</span>
      </div>
    </SlideFrame>
  );
}

window.Slide12Market = Slide12Market;
window.Slide13Traction = Slide13Traction;
window.Slide14Origin = Slide14Origin;
window.Slide15Team = Slide15Team;
window.Slide16Close = Slide16Close;
