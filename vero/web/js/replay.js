// ============================================================================
// THE 3D REPLAY.
//
// The replay is the same kind of 3D travel view as the one in 控制: pick a
// recording and press play. It has a seek bar, for jumping and for
// watching the progress, and a checkbox that can stop the travel from
// being replayed (it takes effect immediately).
//
// WHAT THIS IS. A recording holds, for every sample, the travel the
// device was at and the pressure at each of the five positions. This
// plays those samples back through the SAME 3D drawing the live view
// uses, so a recording can be watched instead of only read.
//
// THE CHECKBOX. "回放行程变化" -- when it is ON, the drawing's travel
// follows the recording, so the inner cylinder slides as it did when the
// recording was made. When it is OFF, the travel is FROZEN at whatever
// it was when the checkbox was turned off, and only the pressures keep
// moving. That is the point of the option: the operator wants to look at
// one position's pressure history without the travel taking the
// drawing away.
//
// It takes effect IMMEDIATELY -- turning it off mid-playback freezes the
// travel at that moment, and turning it back on lets the travel resume
// from where the recording is.
//
// THE TIME BASE. The playback advances by WALL CLOCK, multiplied by the
// speed, and lands on the nearest recorded sample. It does not advance
// one sample per tick: the samples are not evenly spaced (the recorder
// samples on an interval, but a gap in the telemetry stretches it), so
// stepping by sample index would play the gaps at the wrong speed.
//
// The seek bar is in THOUSANDTHS of the recording's duration, because an
// <input type=range> wants an integer and the duration is a float.
//
// ---------------------------------------------------------------------------
// WHY THE DRAWING IS SMOOTH
//
// MEASURED, on this machine, over all 15 recordings on disk (33,718
// samples): the recorder samples at a mean interval of 0.1013 s --
// 9.87 Hz -- with ZERO gaps over 0.5 s. So the recording holds ten real
// frames a second.
//
// The live 3D view is smooth because the DEVICE sends at up to 30 ms
// (the app's own MAX_FRAME_INTERVAL, so ~33 Hz) and the drawing is
// re-rendered every painted frame. The REPLAY had the same 60 FPS
// renderer, but it was fed the recording's 9.87 Hz samples: so it
// painted sixty frames a second, and 50 of those 60 frames showed a
// value the operator had already seen. The measured content rate was
// 0.3 changes a second in one test and ~10 in another -- either way,
// the drawing SAT STILL between samples, and a drawing that sits still
// and then jumps reads as stutter.
//
// SO THE VALUES ARE INTERPOLATED. Between two recorded samples the
// travel and the pressures are blended, so every painted frame is a
// NEW value and the drawing moves continuously.
//
// WHAT THIS IS NOT. The interpolated values are NOT written anywhere.
// The archive on disk is untouched, the curve chart draws the real
// samples, and the seek bar still lands on a real sample. The
// interpolation is a DRAWING technique: it makes the motion between
// two measurements look like motion instead of a jump. A real device
// moved continuously between its two measurements, so the drawing shows
// the path it must have taken.
//
// The pressures are interpolated too, and for the same reason: a
// finger pressing harder between two samples would have produced a
// continuous reading, and a step in the colour looks like a glitch.
//
// TWO THINGS ARE NOT INTERPOLATED. The SLOT -- which segment the device
// said was triggered -- is a LABEL, not a measurement: it is either 0 or
// 3, and a blend of "not triggered" and "segment 4" is not a thing. And
// the position percentage follows the slot, so it follows the same
// rule: it holds the earlier sample's value until the next real
// sample. Holding is not interpolating: it keeps the drawing honest
// about the one thing that is a fact rather than a measurement.
//
// ---------------------------------------------------------------------------
// THE PER-FRAME COST.
//
// The first version rebuilt the sample index on every frame: it walked
// the whole archive to find the samples that carry a time, and then
// binary-searched it. On a 10,292-sample archive that is 0.033 ms --
// not the cause of anything, but it is per frame and it is pure waste.
// The index is now built ONCE per load and cached, so a frame costs a
// binary search over an array that is already there.

export function createReplay(opts) {
  const {
    // The 3D view's factory, so the replay draws the same picture the
    // live view does.
    makeV3D,
    canvas,
    note,
    meta,
    playBtn,
    stopBtn,
    seek,
    timeEl,
    speedBox,
    travelCheck,
    // WHERE THE PLAYBACK IS, told to the page.
    //
    // The replay knows the second it is playing; the curve card below
    // has to show it. The replay does not draw the curve -- the chart
    // module does -- so the replay cannot draw the playhead either.
    // It says where it is, and the page passes it to the chart.
    onPlayhead,
  } = opts;

  // The recording being replayed, and the samples that matter.
  let rec = null;
  // WHICH recording is loaded -- its name. It is what tells a re-read
  // of the SAME recording apart from a switch to a different one.
  let recLabel = null;
  // The sample index the playback is AT.
  let idx = 0;
  // Is it playing?
  let playing = false;
  // The playback speed.
  let speed = 1;
  // The frozen travel, used when the travel checkbox is off.
  let frozenTravel = null;
  // THE PLAYBACK CLOCK, in seconds from the start of the recording.
  //
  // It is a SEPARATE variable from the sample index, and it has to be.
  // The obvious implementation -- "the time of the current sample, plus
  // the elapsed time since the last frame" -- does not work, and the
  // reason is worth writing down: the current sample's time only moves
  // when the sample index moves, and the sample index only moves when
  // the time moves past it. So the two chase each other in a circle and
  // the playback never leaves the first sample.
  //
  // The clock is therefore accumulated from the wall clock directly, and
  // the sample index is DERIVED from it. One source of truth, and the
  // playback moves at the wall clock's speed whatever the sampling is.
  let clock = 0;
  // The TIMER handle, so only one loop is ever running.
  //
  // A TIMER, not requestAnimationFrame. rAF is the right tool for
  // DRAWING -- it fires once per painted frame -- but it is the wrong
  // tool for a CLOCK. The browser throttles rAF hard when the page is
  // not being painted: a background tab gets about one callback a
  // second, and a page that is scrolled away can be throttled too.
  //
  // That is not a hypothetical here: the playback lives in the
  // "records" tab, and while the operator is watching the curve -- or
  // has scrolled up to read something -- the playback stalled, because
  // the clock was driven by the paint rate. The playback has to run on
  // the wall clock, not on the paint rate.
  //
  // The drawing itself still goes through the 3D view's own rAF loop,
  // which is where it belongs.
  let rafId = null;
  // The wall-clock time of the last frame, so the delta is real.
  let lastNow = null;
  // THE FRAME RATE IS THE DISPLAY'S, 60 FPS -- THE SAME AS THE LIVE VIEW.
  //
  // The replay runs at the same 60 FPS as the live 3D view.
  //
  // The playback was on a 60 ms timer -- about 17 frames a second --
  // and that is visibly not the live view's 60. The live view is driven
  // by requestAnimationFrame, which fires once per PAINTED frame, and the
  // browser paints at 60 Hz (or the display's own rate).
  //
  // So the replay is driven by requestAnimationFrame too, for the same
  // reason: the drawing has to be as smooth as the live drawing, and a
  // timer's rate is not the display's rate.
  //
  // The clock is still the WALL clock: the frame's delta is the real
  // elapsed time, so a frame that arrives late (because the browser was
  // busy) advances the playback by the time that actually passed.

  // ------------------------------------------------------------------ the
  // CACHED SAMPLE INDEX.
  //
  // The samples that carry a time, and their times, in order. Built ONCE
  // per load, not once per frame: the frame used to walk the whole
  // archive to build this, which on a 10,292-sample archive cost
  // 0.033 ms per frame for no reason at all.
  let sampleIdx = [];
  let sampleT = [];
  // The first sample's time, so a moment in the recording can be turned
  // into an absolute time on the sample's own clock.
  let tBase = 0;

  // The 3D view. It is created when there is something to draw, and
  // disposed when there is not, so picking a dozen archives does not
  // leave a dozen WebGL contexts alive.
  let view = null;

  // WHETHER THE REPLAY IS ON SCREEN.
  //
  // The live 3D view has had this switch since the last round, and the
  // replay did not: with the 数据记录 tab open, the live view was still
  // issuing draw calls for a canvas nobody could see -- 363 draws a
  // second -- while the replay drew its own. The browser's
  // visibilitychange only fires when the WHOLE PAGE is hidden, so a
  // hidden PANEL is never told anything.
  //
  // So the replay is told, the same way the live view is.
  let onScreen = true;

  function ensureView() {
    if (view) return view;
    view = makeV3D(canvas, note);
    return view;
  }

  function dropView() {
    if (!view) return;
    try { view.dispose(); } catch (e) {}
    view = null;
  }

  // The canvas has to follow the layout box, and a hidden panel's
  // layout box is 0x0. The view is created without being resized, so a
  // replay started while the panel was hidden came up blank -- and
  // stayed blank, because nothing ever told the view the size had
  // changed.
  function resizeView() {
    if (!view) return;
    try {
      if (typeof view.resize === "function") view.resize();
      else if (typeof view.setSize === "function") view.setSize();
    } catch (e) {}
  }

  // ------------------------------------------------------------------ the
  // samples. A recording's samples all carry a time; the ones that do
  // not (a sample the recorder could not time) are skipped rather than
  // treated as zero, because a zero would make the playback jump.
  //
  // The index is rebuilt here and cached, so every later frame reads it
  // instead of walking the archive again.
  function buildIndex() {
    sampleIdx = [];
    sampleT = [];
    samplePos = new Map();
    tBase = 0;
    if (!rec) return;
    const t = rec.t || [];
    for (let i = 0; i < t.length; i++) {
      if (t[i] != null && isFinite(t[i])) {
        if (!sampleT.length) tBase = t[i];
        samplePos.set(i, sampleIdx.length);
        sampleIdx.push(i);
        sampleT.push(t[i]);
      }
    }
  }

  function times() {
    return sampleIdx;
  }

  function duration() {
    if (!sampleT.length) return 0;
    return Math.max(0, sampleT[sampleT.length - 1] - sampleT[0]);
  }

  // The sample at or just before a moment in the recording.
  //
  // The search is over the CACHED index, so it is a binary search over
  // an array that is already built -- not a walk of the whole archive.
  function sampleAt(seconds) {
    if (!sampleT.length) return -1;
    const want = sampleT[0] + seconds;
    let lo = 0, hi = sampleT.length - 1, best = sampleIdx[0];
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (sampleT[mid] <= want) { best = sampleIdx[mid]; lo = mid + 1; }
      else hi = mid - 1;
    }
    return best;
  }

  // ------------------------------------------------------------------ the
  // INTERPOLATION.
  //
  // The fraction between the sample AT the moment and the one AFTER it,
  // in 0..1. This is what makes a frame's value a blend rather than a
  // repeat: the recording's samples are 0.1 s apart and the frames are
  // 1/60 s apart, so six frames share each sample, and the blend gives
  // each of those six a different value.
  //
  // It returns 0 at the end of the recording -- there is no sample after
  // the last one, so there is nothing to blend towards and the drawing
  // holds the last real value.
  function blendAt(seconds) {
    if (sampleT.length < 2) return 0;
    const last = sampleT.length - 1;
    // The index of the last sample at or before the moment.
    let lo = 0, hi = last, at = 0;
    const want = sampleT[0] + seconds;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (sampleT[mid] <= want) { at = mid; lo = mid + 1; }
      else hi = mid - 1;
    }
    if (at >= last) return 0;
    const span = sampleT[at + 1] - sampleT[at];
    if (!(span > 0)) return 0;
    const f = (want - sampleT[at]) / span;
    return f < 0 ? 0 : (f > 1 ? 1 : f);
  }

  // The position of a sample INDEX in the cached list. The cached
  // list is built from the archive's sample indices, so the position
  // is what the blend needs -- and finding it with indexOf() would be
  // a walk of the whole archive, once per pressure, once per frame.
  // The map is built with the index, so it is O(1).
  let samplePos = new Map();

  // A number from a column, blended between the two samples around the
  // moment. A missing value -- a sample the recorder could not read --
  // is NOT blended: the earlier sample's value is held until the next
  // REAL value arrives, because a gap in the data is information and
  // bridging it with a made-up number would hide it.
  function blended(column, seconds, fallback) {
    if (!rec || !Array.isArray(column)) return fallback;
    const at = sampleAt(seconds);
    if (at < 0) return fallback;
    const v0 = column[at];
    if (v0 == null || !isFinite(v0)) return fallback;
    const f = blendAt(seconds);
    if (f <= 0) return v0;
    // The sample AFTER the current one, in the SAME column.
    const pos = samplePos.get(at);
    let next = null;
    if (pos != null) {
      for (let k = pos + 1; k < sampleIdx.length; k++) {
        const cand = column[sampleIdx[k]];
        if (cand != null && isFinite(cand)) { next = cand; break; }
      }
    }
    if (next == null) return v0;
    return v0 + (next - v0) * f;
  }

  // The pressure map at a moment, in the shape the 3D view wants:
  // {"位置1": n, ...}. The recording carries them under "press", one
  // array per slot, and the slot names are the recording's own.
  //
  // The pressures ARE blended -- a finger pressing harder between two
  // samples produced a continuous reading, and a step in the colour
  // reads as a glitch rather than as a measurement.
  function pressuresAt(seconds) {
    const out = {};
    const press = (rec && rec.press) || {};
    for (const name of Object.keys(press)) {
      const arr = press[name];
      if (Array.isArray(arr)) {
        const v = blended(arr, seconds, null);
        if (v != null) out[name] = v;
      }
    }
    return out;
  }

  // The travel at a moment. The recording carries the target and the
  // active travel; the ACTIVE one is what the device really did, so it
  // is the one the drawing shows.
  function travelAt(seconds) {
    if (!rec) return 0;
    const a = rec.travel_active;
    const b = rec.travel_target;
    if (Array.isArray(a)) {
      const v = blended(a, seconds, null);
      if (v != null) return v;
    }
    if (Array.isArray(b)) {
      const v = blended(b, seconds, null);
      if (v != null) return v;
    }
    return 0;
  }

  // The active slot at a moment -- which segment the device said was
  // being pressed. It is drawn as the note's "第 N 段".
  //
  // NOT INTERPOLATED. The slot is a LABEL, not a measurement: it is
  // either "nothing" or "segment 4", and a blend of the two is not a
  // thing that could have happened. The EARLIER sample's slot is held
  // until the next real sample says otherwise.
  function slotAt(seconds) {
    if (!rec || !Array.isArray(rec.slot)) return null;
    const at = sampleAt(seconds);
    if (at < 0) return null;
    const v = rec.slot[at];
    return (v == null) ? null : v;
  }

  // ------------------------------------------------------------------ draw

  function draw() {
    if (!rec || !rec.count) return;
    // The moment in the recording that is being drawn. It is the
    // CLOCK, not the sample index: the drawing is between samples, and
    // that is the whole point.
    const at = clock;
    const travel = (travelCheck && !travelCheck.checked && frozenTravel != null)
      ? frozenTravel
      : travelAt(at);
    const named = pressuresAt(at);
    try {
      ensureView().update(travel, slotAt(at), named);
    } catch (e) {}
    // The clock.
    const t = rec.t || [];
    const first = (t.length && t[0] != null) ? t[0] : 0;
    const rel = at;
    if (timeEl) {
      timeEl.textContent = rel.toFixed(2) + " / " + duration().toFixed(2) + " s";
    }
    // The seek bar.
    if (seek) {
      const d = duration();
      seek.value = d > 0 ? Math.round(rel / d * 1000) : 0;
      // The seek bar's VALUE TEXT follows it. The bar is in
      // thousandths, so the text is the fraction of the recording --
      // "12.40 / 86.28 s" -- and a reader who cannot see the bar still
      // knows where the replay is.
      // The guard is because the element may be a plain object: the
      // replay module is also run in node by the regression suite, and
      // a stub without setAttribute would throw.
      if (typeof seek.setAttribute === "function") {
        seek.setAttribute("aria-valuetext",
                          rel.toFixed(2) + " / " + d.toFixed(2) + " s");
      }
    }
    // AND THE CURVE BELOW IS TOLD WHERE THE PLAYBACK IS.
    //
    // The 3D view and the curve card are the same recording seen two
    // ways, and without this the two had no link: the view was at one
    // moment and the curve showed the whole recording with nothing
    // saying which part of it was on screen.
    //
    // It is reported HERE, in draw(), because draw() is the one place
    // that knows the clock -- play(), pause(), seekTo() and the loop
    // all end up here, so this one call covers every way the playback
    // can move.
    if (typeof onPlayhead === "function") {
      try { onPlayhead(rel); } catch (e) {}
    }
  }

  // ------------------------------------------------------------------ play

  let frameCount = 0;
  function loop(now) {
    if (!playing) return;
    // A REPLAY THAT IS NOT ON SCREEN DOES NOT DRAW.
    //
    // The clock keeps its place -- the operator comes back to the tab
    // and the playback continues where it was -- but the frames are not
    // painted, because nobody can see them. A hidden panel's canvas is
    // 0x0, so the draw was not even producing a picture; it was
    // burning the frame budget.
    if (!onScreen) {
      rafId = requestAnimationFrame(loop);
      return;
    }
    frameCount++;
    // The frame is counted AFTER the on-screen test, so the count is
    // the number of frames actually PAINTED. It used to be counted
    // before, which meant the number grew while the replay was hidden
    // -- and a probe asking "how many frames were painted?" would be
    // told about frames nobody ever saw.
    // The frame's own timestamp, so the delta is the REAL elapsed time
    // and not the frame's nominal period. A frame that arrives late
    // (because the browser was busy) must advance the clock by the
    // time that actually passed, or the playback runs slow.
    if (now == null) now = performance.now();
    if (lastNow == null) lastNow = now;
    const dt = (now - lastNow) / 1000;
    lastNow = now;
    // The playback clock advances by the wall clock times the speed.
    clock += dt * speed;
    const dur = duration();
    if (clock >= dur) {
      clock = dur;
      idx = sampleAt(dur);
      draw();
      stop();
      return;
    }
    idx = sampleAt(clock);
    draw();
    rafId = requestAnimationFrame(loop);
  }

  function play() {
    if (!rec || !rec.count) return;
    // Playing from the very end restarts from the beginning, rather
    // than sitting there doing nothing.
    const dur = duration();
    if (dur > 0 && clock >= dur - 1e-3) {
      idx = sampleAt(0);
      clock = 0;
    }
    playing = true;
    lastNow = null;
    if (playBtn) playBtn.textContent = "暂停";
    if (stopBtn) stopBtn.disabled = false;
    if (rafId != null) cancelAnimationFrame(rafId);
    rafId = requestAnimationFrame(loop);
  }

  function pause() {
    playing = false;
    if (rafId != null) cancelAnimationFrame(rafId);
    rafId = null;
    if (playBtn) playBtn.textContent = "播放";
  }

  function stop() {
    pause();
    if (stopBtn) stopBtn.disabled = true;
  }

  // ------------------------------------------------------------------ the
  // API.

  const api = {
    // Load a recording for replay. `curve` is what /api/records/<name>
    // returns.
    load(curve, label) {
      // Is this the SAME recording as the one already loaded? The page
      // re-reads the recording that is still being written, because it
      // grows -- and a re-read must NOT take the playback back to the
      // start. The first version of this reset the index and the clock
      // on every load, so the live recording's playback restarted from
      // zero every two seconds.
      const same = (label != null && recLabel === label);
      const wasPlaying = playing;
      const keepClock = clock;
      const keepIdx = idx;
      rec = curve || null;
      if (label != null) recLabel = label;
      // The sample index is rebuilt here, ONCE, and every frame reads
      // it. The archive that is still being written grows, so this is
      // the one place the index is rebuilt -- and it is rebuilt on the
      // load, not on the frame.
      buildIndex();
      if (same) {
        // The SAME recording, with more samples in it. The 3D view is
        // KEPT -- dropping it and making a new one would tear down the
        // WebGL context and build a new one every two seconds, which
        // is both slow and visible as a flicker.
        idx = Math.min(keepIdx, Math.max(0, (rec && rec.count ? rec.count : 1) - 1));
        clock = keepClock;
        if (wasPlaying) play();
      } else {
        // A DIFFERENT recording: the previous view goes away with it.
        dropView();
        idx = 0;
        clock = 0;
        frozenTravel = null;
        pause();
        if (stopBtn) stopBtn.disabled = true;
        if (playBtn) playBtn.textContent = "播放";
      }
      if (!rec || !rec.count) {
        if (meta) meta.textContent = label ? (label + "：没有采样点") : "未选择档案";
        if (note) note.textContent = "选择一条记录后可播放";
        if (playBtn) playBtn.disabled = true;
        if (seek) { seek.disabled = true; seek.value = "0"; }
        if (timeEl) timeEl.textContent = "— / —";
        return;
      }
      if (playBtn) playBtn.disabled = false;
      if (seek) seek.disabled = false;
      if (meta) {
        meta.textContent = (label || "当前档案") + " · " + rec.count
          + " 个采样点 · " + duration().toFixed(2) + " 秒";
      }
      // The FIRST frame is drawn straight away, so the operator sees
      // the shape of the recording before starting it.
      //
      // THE INDEX IS NOT RESET HERE.
      //
      // This line used to be an unconditional `idx = 0`, and it ran
      // AFTER the branch above had already computed the right value --
      // the same-recording branch clamps the index to the new sample
      // count and the different-recording branch sets it to 0 itself.
      //
      // So the unconditional line threw away the clamp, and the live
      // recording -- which is re-read every two seconds and is
      // therefore always the "same" branch -- had its index reset to
      // zero every two seconds. The picture did not jump, because the
      // picture follows the CLOCK and the clock was preserved; but the
      // index the page reports changed under the operator, and it is
      // the index the seek bar and the tests read.
      //
      // The index is derived from the clock everywhere else, so the
      // right thing here is to DERIVE it: the index follows the clock,
      // and the clock was already decided above.
      idx = sampleAt(clock);
      // The frozen travel starts as NULL, not as the first frame's
      // travel. It used to be set to travelAt(0), and that was wrong:
      // the frozen travel is supposed to be "the travel at the moment
      // the operator froze it", and seeding it with the recording's
      // FIRST travel meant that unchecking the box -- which does not
      // call freezeTravel() if the handler is broken -- froze the
      // travel at the beginning of the recording instead of where the
      // operator was looking. The checkbox then looked like it worked
      // while actually showing the wrong frame.
      frozenTravel = null;
      // A LOAD DOES NOT PAINT A HIDDEN PANEL.
      //
      // The page re-reads the recording that is being written every two
      // seconds, and each re-read ends here. When the panel is hidden
      // -- which it is, whenever the operator is on another tab -- the
      // draw went to a 0x0 canvas and produced no picture, while
      // costing a WebGL frame.
      if (onScreen) draw();
    },

    play, pause, stop,

    playing() { return playing; },

    // The index the playback is at, for the tests.
    index() { return idx; },
    travel() {
      return (travelCheck && !travelCheck.checked && frozenTravel != null)
        ? frozenTravel : travelAt(clock);
    },
    // The travel the drawing is showing, which is the FROZEN one when
    // the checkbox is off. This is what the test checks.
    shownTravel() {
      return (travelCheck && !travelCheck.checked && frozenTravel != null)
        ? frozenTravel : travelAt(clock);
    },
    pressures() { return pressuresAt(clock); },
    duration() { return duration(); },

    // Freeze the travel at the current moment. Called when the
    // checkbox is turned OFF, so the travel stops where the operator
    // was looking -- not where the recording was when it started.
    freezeTravel() { frozenTravel = travelAt(clock); },
    // Let the travel follow the recording again.
    thawTravel() { frozenTravel = null; },

    setSpeed(v) { speed = v; },
    speed() { return speed; },

    // The internals, for the measurement scaffolding.
    // The frames painted since the playback started. The measurement
    // scaffolding reads it to check the playback's RATE, not just that
    // it moves: a playback that advanced at 17 Hz would still pass a
    // test that only checks "the index changed".
    frameCount: () => frameCount,
    // The internals, for the measurement scaffolding: the sample
    // lookup and the sample list, so a probe can check that the
    // playback lands on the sample it claims to.
    _sampleAt: sampleAt,
    _times: times,
    // The internals, for the measurement scaffolding: the moment the
    // drawing is AT, and the blend factor at that moment. A probe uses
    // them to check that the drawing is BETWEEN two samples and not
    // sitting on one -- which is the whole difference between a smooth
    // replay and a stuttering one.
    _clock: () => clock,
    _blendAt: blendAt,
    // The moment in the recording, and the value the drawing shows.
    _travelAt: travelAt,
    _pressuresAt: pressuresAt,

    // WHETHER THE REPLAY IS ON SCREEN.
    //
    // The page tells the replay when the tab changes, the same way it
    // tells the live 3D view. Coming back onto the tab resizes the
    // view (a hidden panel's canvas is 0x0) and redraws the current
    // frame, so the operator sees the recording rather than a blank
    // canvas.
    setActive(on) {
      onScreen = !!on;
      if (onScreen) {
        resizeView();
        try { draw(); } catch (e) {}
      }
      return onScreen;
    },
    running() { return playing && onScreen; },
    onScreen() { return onScreen; },
    // Tell the view the canvas changed size.
    resize() { resizeView(); },

    // Seek to a fraction of the recording, 0..1.
    seekTo(frac) {
      if (!rec || !rec.count) return;
      const f = Math.max(0, Math.min(1, frac));
      clock = f * duration();
      idx = sampleAt(clock);
      draw();
    },
  };
  return api;
}

// The wiring, done here so the page does not have to know the details.
export function wireReplay(opts) {
  const replay = createReplay(opts);

  if (opts.playBtn) {
    opts.playBtn.addEventListener("click", () => {
      if (replay.playing()) replay.pause(); else replay.play();
    });
  }
  if (opts.stopBtn) {
    opts.stopBtn.addEventListener("click", () => {
      replay.stop();
      replay.seekTo(0);
    });
  }
  if (opts.seek) {
    // The seek bar is in thousandths, so the fraction is that divided
    // by a thousand.
    opts.seek.addEventListener("input", () => {
      replay.seekTo(Number(opts.seek.value) / 1000);
    });
  }
  if (opts.speedBox) {
    for (const b of opts.speedBox.children) {
      b.addEventListener("click", () => {
        for (const x of opts.speedBox.children) x.classList.remove("on");
        b.classList.add("on");
        replay.setSpeed(Number(b.dataset.s));
      });
    }
  }
  if (opts.travelCheck) {
    // REAL TIME: the change takes effect the moment
    // the box is ticked, not at the next frame boundary.
    opts.travelCheck.addEventListener("change", () => {
      if (opts.travelCheck.checked) replay.thawTravel();
      else replay.freezeTravel();
    });
  }
  return replay;
}
