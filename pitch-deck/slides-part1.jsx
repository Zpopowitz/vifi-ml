// =========================================================================
// ViFi — Individual slide components
// =========================================================================

// ---------- 01: Title ----------
function Slide01Title() {
  return (
    <SlideFrame dark noPad>
      {/* Ambient waveform, bottom — quiet, one motion moment */}
      <svg viewBox="0 0 1920 180" width="100%" height="180" preserveAspectRatio="none"
        style={{position: 'absolute', left: 0, right: 0, bottom: 120, opacity: 0.7}}>
        <path d="M0 90 L420 90 L470 54 L510 132 L540 22 L572 148 L610 90 L900 90 L950 66 L990 120 L1030 90 L1320 90 L1368 74 L1405 106 L1440 90 L1920 90"
          stroke={C.signal} strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round"
          strokeDasharray="3000" style={{animation: 'vs-wave 4s linear infinite'}}/>
      </svg>
      <style>{`@keyframes vs-wave { 0% { stroke-dashoffset: 3000 } 100% { stroke-dashoffset: 0 } }`}</style>

      <div style={{
        padding: `96px 120px 80px`,
        height: '100%', display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
        position: 'relative', zIndex: 1, boxSizing: 'border-box',
      }}>
        <div style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between'}}>
          <img src="assets/logo/vifi-logo-dark.png" alt="ViFi" style={{height: 84, width: 'auto', display: 'block'}}/>
          <MicroLabel dark style={{color: C.onDark2, fontSize: 26}}>Y Combinator · S26 application</MicroLabel>
        </div>

        <div>
          <h1 style={{
            fontFamily: FONT_SANS, fontSize: 184, fontWeight: 500,
            lineHeight: 0.96, letterSpacing: '-0.035em', color: C.onDark1,
            margin: 0, maxWidth: 1700, textWrap: 'balance',
          }}>
            Contactless hospital vitals from WiFi signals.
          </h1>
          <div style={{
            marginTop: 56, fontSize: 48, lineHeight: 1.32,
            color: C.onDark2, maxWidth: 1600, fontWeight: 400,
          }}>
            <span style={{color: C.signal, fontWeight: 500}}>4.15 bpm cross-session heart-rate MAE</span> on real $44 ESP32-S3 hardware. One sensor per bed. No wires, no adhesives, no line of sight.
          </div>
        </div>

        <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', fontFamily: FONT_SANS, fontSize: 26, color: C.onDark3, letterSpacing: '0.08em', textTransform: 'uppercase', fontWeight: 500}}>
          <span>Zach Popowitz · Founder</span>
          <span>April 2026</span>
        </div>
      </div>
    </SlideFrame>
  );
}

// ---------- 02: Problem ----------
function Slide02Problem() {
  return (
    <SlideFrame>
      <MicroLabel>01 · The problem</MicroLabel>
      <div style={{marginTop: SPACING.titleGap, display: 'grid', gridTemplateColumns: '1.15fr 1fr', gap: 100, alignItems: 'stretch', flex: 1}}>
        <SlideTitle size={140} style={{alignSelf: 'center'}}>
          Hospitals can't watch everyone continuously.
        </SlideTitle>
        <div style={{fontFamily: FONT_SERIF, fontSize: 52, lineHeight: 1.42, color: C.fg1, borderLeft: `2px solid ${C.ink}`, paddingLeft: 56, alignSelf: 'center'}}>
          <p style={{margin: '0 0 32px'}}>
            Outside the ICU, patients are checked manually every 4 to 8 hours. For 23 of every 24 hours, no one is watching.
          </p>
          <p style={{margin: 0, color: C.fg2}}>
            The signs of sepsis, bacteremia, respiratory decline, and cardiac events appear between rounds.
          </p>
        </div>
      </div>
      <Footer label="The problem" pageNum="02 / 16"/>
    </SlideFrame>
  );
}

// ---------- 03: Big stat — 4.15 bpm headline ----------
function Slide03Stat() {
  return (
    <SlideFrame dark>
      <MicroLabel dark style={{color: C.signal}}>Headline result · April 2026</MicroLabel>
      <div style={{flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center', marginTop: 32}}>
        <div style={{display: 'flex', alignItems: 'baseline', gap: 48, marginBottom: 44}}>
          <div style={{
            fontFamily: FONT_SANS, fontSize: 460, fontWeight: 500,
            lineHeight: 0.82, letterSpacing: '-0.05em', color: C.signal,
            fontVariantNumeric: 'tabular-nums',
          }}>4.15</div>
          <div style={{
            fontFamily: FONT_SANS, fontSize: 116, fontWeight: 300,
            lineHeight: 1, letterSpacing: '-0.02em', color: C.onDark2,
          }}>bpm MAE</div>
        </div>
        <SlideTitle dark size={84} style={{maxWidth: 1700}}>
          cross-session heart rate, on $44 of commodity hardware.
        </SlideTitle>
        <div style={{marginTop: 40, fontSize: 34, lineHeight: 1.5, color: C.onDark2, maxWidth: 1600}}>
          Two ESP32-S3 nodes vs Polar H10 chest strap, leave-one-session-out cross-validation.
          PhaseBeat (INFOCOM 2017) hit similar accuracy on $500-per-node Intel 5300 NICs. We replicated it on hardware that costs roughly 1/25th as much.
        </div>
      </div>
      <Footer label="Headline result" pageNum="03 / 16" dark/>
    </SlideFrame>
  );
}

// ---------- 04: Existing monitoring fails ----------
function Slide04Existing() {
  const reasons = [
    {n: '01', title: 'Too expensive', body: '$3,000–$5,000 per bed. Hospitals can only afford it for the ~10% of beds in the ICU.'},
    {n: '02', title: 'Too invasive', body: 'Wires, adhesives, and patient compliance. Elderly patients remove them. Sensors fall off.'},
    {n: '03', title: 'Too narrow', body: 'Rounds every 4–8 hours. Continuous monitors cover one vital, at one spot, for one patient.'},
  ];
  return (
    <SlideFrame>
      <MicroLabel>01 · The problem, continued</MicroLabel>
      <div style={{marginTop: SPACING.titleGap, flex: 1, display: 'flex', flexDirection: 'column'}}>
        <SlideTitle style={{maxWidth: 1500, marginBottom: 64}} size={104}>
          The monitors we have are expensive, invasive, and don't scale.
        </SlideTitle>
        <div style={{display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 32, marginTop: 12, flex: 1}}>
          {reasons.map(r => (
            <div key={r.n} style={{
              background: C.paper2, border: `1px solid ${C.paper3}`,
              borderRadius: 4, padding: '52px 44px 56px',
              display: 'flex', flexDirection: 'column', gap: 28,
            }}>
              <div style={{fontFamily: FONT_MONO, fontSize: 30, color: C.fg3, letterSpacing: '0.04em'}}>{r.n}</div>
              <div style={{fontSize: 56, fontWeight: 600, lineHeight: 1.1, letterSpacing: '-0.015em'}}>{r.title}</div>
              <div style={{fontSize: 30, lineHeight: 1.45, color: C.fg2}}>{r.body}</div>
            </div>
          ))}
        </div>
      </div>
      <Footer label="The problem" pageNum="04 / 16"/>
    </SlideFrame>
  );
}

// ---------- 05: Section break — Our solution ----------
function Slide05Section() {
  return (
    <SlideFrame dark noPad>
      <div style={{
        padding: `96px 120px 80px`,
        height: '100%', display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
        boxSizing: 'border-box',
      }}>
        <div style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between'}}>
          <img src="assets/logo/vifi-logo-dark.png" alt="ViFi" style={{height: 72, width: 'auto'}}/>
          <MicroLabel dark style={{color: C.signal, fontSize: 26}}>Part 02 · What we built</MicroLabel>
        </div>
        <div>
          <h2 style={{
            fontFamily: FONT_SANS, fontSize: 168, fontWeight: 500,
            lineHeight: 0.98, letterSpacing: '-0.035em', color: C.onDark1,
            margin: 0, maxWidth: 1750, textWrap: 'balance',
          }}>
            One sensor.<br/>Every vital sign the room can tell us.
          </h2>
          <div style={{marginTop: 56, fontSize: 44, lineHeight: 1.38, color: C.onDark2, maxWidth: 1500}}>
            Two $15 chips. WiFi signals. A software pipeline that turns the radio noise between them into vitals.
          </div>
        </div>
        <div style={{fontFamily: FONT_MONO, fontSize: 26, color: C.onDark3, letterSpacing: '0.08em'}}>05 / 16 · OUR SOLUTION</div>
      </div>
    </SlideFrame>
  );
}

// ---------- 06: How it works ----------
function Slide06HowItWorks() {
  const steps = [
    {n: '01', title: 'Two ESP32-S3 chips, 192 subcarriers', body: 'TX and RX exchange WiFi packets at 70–100 pps. Channel State Information (CSI) reflects chest-wall motion.'},
    {n: '02', title: 'Bandpass + FFT + peak refinement', body: 'Variance-rank top subcarriers, 0.1–3 Hz Butterworth, 4× zero-padded FFT, parabolic peaks in cardiac and respiratory bands.'},
    {n: '03', title: 'XGBoost regressor → vitals', body: '9-dimensional feature vector → HR (validated), RR (pending GT), presence (shipped), more on the same stream.'},
  ];
  return (
    <SlideFrame>
      <MicroLabel>02 · How it works</MicroLabel>
      <SlideTitle style={{marginTop: SPACING.titleGap}}>How it works.</SlideTitle>

      <div style={{
        marginTop: 36,
        background: C.paper2, border: `1px solid ${C.paper3}`,
        borderRadius: 8, padding: '28px 48px',
      }}>
        <img src="assets/illustrations/signal-path.svg" alt="" style={{width: '100%', height: 'auto', display: 'block', maxHeight: 340}}/>
      </div>

      <div style={{marginTop: 40, display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 48, flex: 1}}>
        {steps.map(s => (
          <div key={s.n}>
            <div style={{fontFamily: FONT_MONO, fontSize: 28, color: C.fg3, letterSpacing: '0.04em'}}>{s.n}</div>
            <div style={{marginTop: 14, fontSize: 34, fontWeight: 600, lineHeight: 1.2, letterSpacing: '-0.005em'}}>{s.title}</div>
            <div style={{marginTop: 12, fontSize: 24, lineHeight: 1.5, color: C.fg2}}>{s.body}</div>
          </div>
        ))}
      </div>
      <Footer label="How it works" pageNum="06 / 16"/>
    </SlideFrame>
  );
}

// ---------- 07: Capabilities grid ----------
function Slide07Capabilities() {
  const caps = [
    {icon: 'heart-pulse',    title: 'Heart rate',        status: 'shipped', body: '4.15 bpm cross-session MAE on real ESP32-S3 hardware. April 2026.'},
    {icon: 'wind',           title: 'Respiratory rate',  status: 'shipped', body: 'Pipeline shipped. RR ground-truth captures next.'},
    {icon: 'user-check',     title: 'Presence',          status: 'shipped', body: 'Variance-threshold detection. Live API endpoint.'},
    {icon: 'alert-triangle', title: 'Fall detection',    status: 'dev',     body: 'In development. Wellness-grade, no FDA needed.'},
    {icon: 'activity',       title: 'Apnea detection',   status: 'dev',     body: 'In development. Pulse-ox as ground truth.'},
    {icon: 'footprints',     title: 'Gait analysis',     status: 'planned', body: 'Planned. Same pair of ESP32 nodes.'},
  ];
  return (
    <SlideFrame>
      <MicroLabel>02 · Capabilities</MicroLabel>
      <SlideTitle style={{marginTop: SPACING.titleGap, maxWidth: 1700}} size={80}>
        One sensor. Every vital sign the room can tell us.
      </SlideTitle>
      <div style={{marginTop: 32, display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gridTemplateRows: '1fr 1fr', gap: 20, flex: 1, minHeight: 0}}>
        {caps.map(c => (
          <div key={c.title} style={{
            background: C.paper2, border: `1px solid ${C.paper3}`,
            borderRadius: 4, padding: '28px 30px', display: 'flex', flexDirection: 'column', gap: 18,
          }}>
            <div style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between'}}>
              <img src={`assets/icons/${c.icon}.svg`} width="48" height="48" alt="" style={{opacity: 0.88}}/>
              <StatusChip status={c.status}/>
            </div>
            <div>
              <div style={{fontSize: 36, fontWeight: 600, lineHeight: 1.1, letterSpacing: '-0.01em'}}>{c.title}</div>
              <div style={{marginTop: 10, fontSize: 24, color: C.fg2, lineHeight: 1.4, maxWidth: 'none'}}>{c.body}</div>
            </div>
          </div>
        ))}
      </div>
      <div style={{marginTop: 20, fontSize: 26, color: C.fg2, maxWidth: 1400}}>
        All six run on the same pair of chips. Adding a new vital means software, not wiring.
      </div>
      <Footer label="Capabilities" pageNum="07 / 16"/>
    </SlideFrame>
  );
}

// ---------- 08: Real-hardware result (alternate render of slide 08; not mounted) ----------
function Slide08Result() {
  return (
    <SlideFrame dark>
      <MicroLabel dark style={{color: C.signal}}>03 · Real-hardware result · April 2026</MicroLabel>
      <SlideTitle dark style={{marginTop: SPACING.titleGap, maxWidth: 1700}} size={88}>
        It works on $44 of hardware.
      </SlideTitle>
      <div style={{marginTop: 40, fontSize: 30, color: C.onDark2}}>(Reserved alternate slide; not currently mounted.)</div>
      <Footer label="Real-hardware result" pageNum="08 / 16" dark/>
    </SlideFrame>
  );
}

window.Slide01Title = Slide01Title;
window.Slide02Problem = Slide02Problem;
window.Slide03Stat = Slide03Stat;
window.Slide04Existing = Slide04Existing;
window.Slide05Section = Slide05Section;
window.Slide06HowItWorks = Slide06HowItWorks;
window.Slide07Capabilities = Slide07Capabilities;
window.Slide08Result = Slide08Result;
