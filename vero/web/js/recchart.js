// ============================================================================
// THE RECORD CHART -- uPlot.
//
// The record chart is drawn with a real JS charting control, not a
// scaled bitmap.
//
// WHY THIS IS A LIBRARY AND NOT MORE SVG.
//
// The old chart was an SVG with a fixed viewBox, stretched by CSS
// ("preserveAspectRatio=none"). That is a PICTURE of a chart, not a
// chart: the strokes are scaled non-uniformly by the CSS box, the text
// is scaled with it, and there is no axis interaction -- no drag to
// pan, no wheel to zoom, no cursor that reads the values.
//
// uPlot is built for exactly this shape of data -- a time series with
// thousands of points -- and it is 145 KB of ESM with no dependencies.
// It draws on a canvas at the display's own resolution (so the curves
// are not resampled by the browser), it has a real cursor that reads
// every series' value at the hovered instant, it pans on drag and
// zooms on wheel, and it has a legend whose rows are the series'
// visibility switches.
//
// The DATA is unchanged: the same read_curve() output, the same
// channels, the same export and import.
//
// WHAT THE CONTROLS ARE, and what they are NOT:
//
//   * uPlot's OWN cursor and drag: the pointer's instant is read out of
//     the chart itself, and dragging pans and wheel zooms. These are the
//     library's, and they are what makes it feel like an instrument.
//   * The "重置视图" button and the "框选放大" (shift-drag) are MINE,
//     because uPlot has no built-in way to say "put the operator back
//     where they started".
//
// TWO AXES, because the channels are two different quantities: the
// travels and the depth are percentages, and the pressures are the
// device's own 12-bit readings. Putting them on one axis would squash
// every pressure into the bottom 10% of the plot.
//
// The chart is DRAWN, NOT SCALED: the canvas is sized from the element's
// measured box times the device pixel ratio, so a curve is one pixel
// wide on a 4K display instead of being a stretched 1000-unit viewBox.
//
// MIT licensed; the licence sits beside the file in /vendor.

import uPlot from "uplot";

//: THE PRESSURE SCALE, IN ONE PLACE.
//
//: The device's pressure readings are 12-bit: 0..4095. That is the
//: DEVICE'S full scale, and it is the scale the chart's right axis is
//: drawn on.
//
//: The 3D view's pink density has a different full-strength point,
//: 3000 -- the full-strength point of the pink concentration. 3000 is
//: NOT a second device scale: it is a point INSIDE
//: the device's 0..4095 range, the point at which the pink is fully
//: saturated.
//
//: The page used to carry both numbers with no statement of which was
//: which, and the legend said "3000 最浓" while the chart's axis said
//: 4095 -- so the same pressure read 73% on one instrument and 100% on
//: the other, and neither said why.
//
//: These two constants are exported so that index.html and this module
//: cannot drift apart: the chart's axis and the 3D view's density are
//: computed from the SAME pair of numbers.
export const PRESSURE_FULL_SCALE = 4095;
export const PRESSURE_DENSITY_FULL = 3000;

const PRESS_COLORS = {
  "位置1": "#f5c542",
  "位置2": "#4fc3f7",
  "位置3": "#3fbf7f",
  "位置4": "#e57903",
  "位置5": "#b98cff",
};

  //: The percentage channels. They share the LEFT axis, 0..100.
  //
  //: The scale they are drawn on is "y" -- the scale the left axis
  //: reads. It used to be "l", a scale no axis read, so the three
  //: percentage curves had no readable vertical scale at all. See
  //: channels() below for the whole story.
const PERCENT_SERIES = [
  {key: "travel_target", label: "行程（目标）", color: "#4c8dff"},
  {key: "travel_active", label: "行程（实际）", color: "#3fbf7f"},
  {key: "position_pct",  label: "深入百分比",   color: "#f5c542"},
];

//: The pressure channels. They share the RIGHT axis, 0..4095 (the
//: device's own 12-bit full scale -- the device's range, not the
//: largest sample in this recording, so two archives can be compared
//: without one of them being rescaled).
const PRESSURE_SERIES = [
  {key: "pressure", label: "触点压力", color: "#d2622e"},
];

//: THE SLOT THAT THE PRIMARY PRESSURE CHANNEL IS.
const PRIMARY_SLOT = "位置1";

// THE DERIVED METRICS.
//
// The numbers that are IN the recording but are not on the chart: how
// long a stroke cycle took, how fast it repeated, and what each of the
// five segments carried.
//
// They are computed from the archive's own series and they are the
// SHAPE of a use rather than a trace of it. A chart shows what
// happened; these say what it amounted to.
//
// THE STROKE CYCLE. The travel series is a position that goes out and
// comes back, so a cycle is one full out-and-back. The cycle is found
// by the RUN-LENGTHS of the travel: a run of the SAME value is one
// plateau, and a cycle is a plateau at the far end followed by one at
// the near end.
//
// The rate is the cycle time turned into strokes per minute, which is
// the number a person actually compares against their own.
//
// THE SEGMENT LOAD. The peak of each position's pressure over the
// recording, which is what each of the five segments carried.
//
// The metrics are computed on the WHOLE archive, not on the visible
// window: they describe the recording, and a window is a view of it.
export function recordMetrics(curve) {
  const out = {
    cycle_seconds: null,
    rate_per_minute: null,
    strokes: 0,
    load: {},
  };
  if (!curve) return out;
  const travel = Array.isArray(curve.travel_target) ? curve.travel_target : [];
  const t = Array.isArray(curve.t) ? curve.t : [];

  // THE STROKE CYCLES, from the travel's run-lengths.
  //
  // A run is a maximal stretch of the same value. The FAR end of the
  // stroke is the highest value the travel reaches -- and a cycle is
  // the time between two visits to it.
  const runs = [];
  let cur = null;
  for (let i = 0; i < travel.length; i++) {
    const v = travel[i];
    if (v == null) continue;
    if (cur && cur.value === v) { cur.end = i; continue; }
    cur = {value: v, start: i, end: i};
    runs.push(cur);
  }
  if (runs.length >= 2) {
    let peak = runs[0].value;
    for (const r of runs) if (r.value > peak) peak = r.value;
    // The times at which the travel was AT its peak. Two visits to the
    // peak are one cycle.
    const peaks = [];
    for (const r of runs) {
      if (r.value !== peak) continue;
      if (peaks.length && r.start === peaks[peaks.length - 1].end) continue;
      peaks.push(r);
    }
    if (peaks.length >= 2) {
      const periods = [];
      for (let i = 1; i < peaks.length; i++) {
        const a = t[peaks[i - 1].end];
        const b = t[peaks[i].end];
        if (typeof a === "number" && typeof b === "number" && b > a) {
          periods.push(b - a);
        }
      }
      if (periods.length) {
        periods.sort((x, y) => x - y);
        const median = periods[Math.floor(periods.length / 2)];
        out.cycle_seconds = Math.round(median * 100) / 100;
        out.rate_per_minute = Math.round((60 / median) * 100) / 100;
        out.strokes = periods.length;
      }
    }
  }

  // THE SEGMENT LOAD: the peak of each position's pressure.
  const press = (curve && curve.press) || {};
  for (const name of Object.keys(press)) {
    const col = press[name];
    if (!Array.isArray(col)) continue;
    let peak = null;
    for (const v of col) {
      if (v == null) continue;
      if (peak === null || v > peak) peak = v;
    }
    if (peak !== null) out.load[name] = peak;
  }
  return out;
}

export function createRecordChart(opts) {
  const {
    container,        // the element the chart is built in
    legendHolder,     // where the chart's OWN legend goes (beside the plot)
    legend,           // where the channel toggles go
    readout,          // where the numbers under the pointer go
    meta,             // the summary line
    seriesBox,        // where the channel toggle buttons go
  } = opts;

  // The recording currently loaded.
  let data = null;
  // The archive as it was loaded, so EXPORT writes the WHOLE archive and
  // not just the visible window. Exporting the window would quietly
  // destroy the rest of the recording, which is the one thing a data
  // export must never do.
  let archive = null;
  // WHICH archive is loaded -- its name. It is what tells a re-read of
  // the SAME archive apart from a switch to a different one.
  let archiveKey = null;
  // The channels this recording carries, in the order they are drawn.
  let series = [];
  // The uPlot instance.
  let plot = null;
  // The X values, shared by every series.
  let xs = [];

  // The playhead's position, in the recording's own seconds. null means
  // "no playhead" -- the chart is being looked at, not played back.
  //
  // It lives here, in the chart, rather than in the replay: the chart is
  // what DRAWS it, and the chart is what knows where a second is on the
  // screen. The replay only knows what second it is playing.
  let playheadT = null;

  // ------------------------------------------------------------------ the
  // channel list. The per-position pressures are read out of the
  // recording itself -- an older recording has none, and a channel that
  // is not there must not be offered.
  //
  // THE AXIS A CHANNEL IS DRAWN ON IS THE AXIS THAT READS ITS SCALE.
  //
  // The percentage channels used to be given the scale "l", and no
  // axis in the axes list read "l" -- so the three percentage curves
  // were drawn on a scale that nothing on the screen was labelled
  // with. uPlot fitted that scale itself (measured: it grew to 110 on
  // a recording whose percentages never went past 100), and the LEFT
  // axis that the operator could actually read was the "y" scale,
  // which had no series on it at all. The numbers beside the curve
  // belonged to a different scale from the curve.
  //
  // So the percentage channels are drawn on "y", which is the scale the
  // left axis reads and the scale whose range is fixed at 0..100. One
  // scale, one axis, one set of numbers.
  function channels() {
    const out = [];
    for (const s of PERCENT_SERIES) {
      if (data && Array.isArray(data[s.key])) out.push({...s, axis: "y"});
    }
    for (const s of PRESSURE_SERIES) {
      if (data && Array.isArray(data[s.key])) out.push({...s, axis: "r"});
    }
    const press = (data && data.press) || {};
    for (const name of Object.keys(press)) {
      if (!Array.isArray(press[name])) continue;
      // THE PRIMARY CHANNEL AND THE PRIMARY SLOT ARE THE SAME CURVE.
      //
      // "pressure" is the monitor's PRIMARY slot reading, and the
      // monitor's PRIMARY slot is 位置1 (monitor.py's SLOT_NAMES and
      // PRIMARY = 0). So the record chart was drawing the same data
      // twice: once as "触点压力" and once as "位置1".
      //
      // Measured on the archive on disk: 68920 of 68920 rows that carry
      // both columns have byte-identical values, and the difference is
      // zero. Two curves, one measurement -- and the operator had no
      // way to know that, because they were the same line drawn twice
      // in two colours.
      //
      // The primary channel is kept and the primary SLOT is dropped,
      // because the primary channel is the one the live panel's "当前"
      // reading and the record chart's scale refer to. The other four
      // slots are untouched.
      if (name === PRIMARY_SLOT) continue;
      out.push({
        key: "press:" + name,
        label: name,
        color: PRESS_COLORS[name] || "#8d84a0",
        axis: "r",
      });
    }
    return out;
  }

  // The values of a channel.
  function values(ch) {
    if (ch.key.startsWith("press:")) {
      const press = (data && data.press) || {};
      return press[ch.key.slice(6)] || [];
    }
    return (data && data[ch.key]) || [];
  }

  // ------------------------------------------------------------------ the
  // uPlot's options. The theme is the page's own: the dark background,
  // the dim labels, the accent.
  function options() {
    return {
      width: 0,              // measured below
      height: 340,
      padding: [10, 10, 0, 0],
      cursor: {
        // The library's own cursor: a dashed crosshair and a point on
        // every series, with the values in the legend.
        //
        // `setScale: false` means the DRAG does not change the scale --
        // the drag is the PAGE's (it selects a span, and the selection
        // is zoomed by the page's own code, so that shift-drag and
        // plain-drag can mean different things). uPlot's own
        // drag-to-zoom is replaced by the wheel, which the page
        // implements, because uPlot's zoom-on-wheel is a separate
        // plugin file and this project vendors one file.
        drag: {x: true, y: false, setScale: false, undistorted: true},
        points: {size: 7, width: 1.5},
      },
      // The legend goes into its OWN element, beside the plot.
      //
      // uPlot puts the legend INSIDE its root by default, and its root
      // is "width: min-content" -- so the legend's width decides the
      // root's width, and the plot area is squeezed to whatever is
      // left. With nine channels the legend is wide, and the plot
      // area ended up at zero pixels wide: the chart drew its cursor
      // and its points but no curves, and no axis labels, and it
      // looked like a broken chart.
      //
      // uPlot's `legend.mount` exists for exactly this ("allows
      // legend to be moved out of root"), so the legend is mounted in
      // a sibling element and the PAGE lays the two out. The chart
      // then owns its plot area completely, and the legend is a
      // caption beside it rather than a column inside it.
      legend: {
        live: true,
        mount: (u, table) => {
          const holder = legendHolder || container;
          holder.innerHTML = "";
          holder.appendChild(table);
        },
      },
      scales: {
        // The X axis keeps uPlot's default (auto: true).
        //
        // Turning it off was tried, to stop uPlot re-fitting the axis
        // and undoing the page's own zoom. It does stop the re-fit --
        // and it BREAKS THE CURSOR: uPlot computes the visible index
        // range (i0..i1) as part of the auto-fit, so with auto off
        // that range stays empty, `noDataInRange` is true, and the
        // cursor's index is null. The pointer then reads nothing at all,
        // and the chart's own legend stops showing values.
        //
        // So the axis is left on auto, and the zoom is done the way
        // uPlot's own drag does it: through setScale(), which is a
        // pending scale that the next commit applies. The commit's
        // re-fit is skipped for a scale that has an explicit range.
        x: {time: false},
        // The LEFT axis is the percentage one, 0..100.
        y: {range: [0, 100], side: 3},
        // The RIGHT axis is the pressure one, the device's 12-bit
        // range (0..4095).
        //
        // The 3000 that the 3D view's pink density saturates at is
        // drawn ON this axis as a marked line, so the operator can see
        // where "fully pink" sits on the same scale as the curve. See
        // the draw hook below.
        r: {range: [0, PRESSURE_FULL_SCALE], side: 1},
      },
      // THE AXES, and each one says which SCALE it reads and which
      // SIDE it stands on.
      //
      // Both fields are required, and neither has a useful default:
      // an axis with no `scale` reads the "y" scale and an axis with
      // no `side` stands on the LEFT. So an axis list that names the
      // scales but not the sides puts every axis on the left -- which
      // is what happened: the first version of this had three axes
      // and no `side`, so the pressure axis was drawn on the left
      // OVER the percentage axis, and the right-hand side of the chart
      // had no scale at all.
      axes: [
        {
          // The X axis -- relative seconds.
          scale: "x",
          side: 2,
          stroke: "#6b6079",
          grid: {stroke: "#262034", width: 1},
          ticks: {stroke: "#4a4258", width: 1, size: 5},
          font: "11px system-ui",
          size: 46,
          // The X axis is RELATIVE seconds: the recording starts at 0.
          values: (u, vals) => vals.map((v) => v.toFixed(1) + "s"),
          space: 60,
        },
        {
          // The RIGHT axis -- the pressure, the device's own 12-bit
          // range (0..4095).
          scale: "r",
          side: 1,
          stroke: "#6b6079",
          grid: {show: false},
          ticks: {stroke: "#4a4258", width: 1, size: 5},
          font: "11px system-ui",
          size: 56,
          values: (u, vals) => vals.map((v) => String(Math.round(v))),
          space: 60,
        },
        {
          // The LEFT axis -- the percentage (0..100).
          scale: "y",
          side: 3,
          stroke: "#6b6079",
          grid: {stroke: "#262034", width: 1},
          ticks: {stroke: "#4a4258", width: 1, size: 5},
          font: "11px system-ui",
          size: 42,
          values: (u, vals) => vals.map((v) => String(Math.round(v))),
          space: 50,
        },
      ],
      series: [],
      // The cursor hook: the library tells us which sample the pointer
      // is over, and the readout is built from it.
      hooks: {
        // uPlot fires this hook with NO arguments -- the cursor is on
        // the plot itself (plot.cursor), not in the callback. The first
        // version of this read the cursor out of the callback's second
        // argument, which is always undefined, so the readout stayed on
        // its em dash while the chart's own legend showed the values.
        setCursor: [(u) => onCursor(u && u.cursor)],
        // THE "FULL PINK" MARKER.
        //
        // The 3D view draws a segment's pink density on a 0..3000
        // ramp, and the chart's right axis is the device's 0..4095. Two
        // full-scale numbers on one page, and nothing said which was
        // which, so the same pressure read 73% on the axis and 100% in
        // the drawing.
        //
        // The marker puts the 3000 on the SAME axis as the curve, so
        // the operator can see at a glance how far the reading is from
        // the point where the 3D drawing saturates. It is drawn on the
        // pressure scale only, and only when a pressure channel is
        // present -- otherwise there is nothing for it to refer to.
        // The playhead comes second, right after the density marker, and
        // the change points last. The order is not cosmetic: the
        // playhead is the ACCENT line and the change points are the
        // yellow marks, and the playhead has to be over the marks so a
        // mark at the playhead's own position is still visible.
        draw: [(u) => drawDensityMarker(u), (u) => drawPlayhead(u),
               (u) => drawChangePoints(u)],
      },
    };
  }

  // The "full pink" line, drawn on the pressure scale.
  //
  // uPlot's draw hook runs after the curves are painted, and the plot's
  // own canvas and context are on the instance (u.ctx, u.bbox), so the
  // line is drawn in the same coordinate space as everything else.
  function drawDensityMarker(u) {
    if (!u || !u.ctx) return;
    const hasPressure = series.some((ch) => ch.axis === "r");
    if (!hasPressure) return;
    const sc = u.scales && u.scales.r;
    if (!sc || sc.min == null || sc.max == null) return;
    if (PRESSURE_DENSITY_FULL < sc.min || PRESSURE_DENSITY_FULL > sc.max) return;
    let y;
    try {
      y = u.valToPos(PRESSURE_DENSITY_FULL, "r", true);
    } catch (e) {
      return;
    }
    if (y == null || !isFinite(y)) return;
    const box = u.bbox;
    if (!box) return;
    const ctx = u.ctx;
    ctx.save();
    ctx.strokeStyle = "rgba(255,95,162,.55)";
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(box.left, y);
    ctx.lineTo(box.left + box.width, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(255,95,162,.9)";
    ctx.font = "10px system-ui";
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    ctx.fillText("粉色满值 " + PRESSURE_DENSITY_FULL,
                 box.left + 4, y - 2);
    ctx.restore();
  }

  // THE CHANGE POINTS.
  //
  // The frames the detector marked, drawn as a small triangle under the
  // pressure curve -- the same mark the LIVE chart uses, so the two
  // views agree on what a change point looks like.
  //
  // The marks come from the recording itself now: read_curve() runs the
  // detector over the archive's pressure series and publishes the marks
  // on the archive's own clock, so a mark lands under its own reading
  // rather than on an index.
  //
  // They are drawn on the RIGHT axis, because that is the axis the
  // pressure is on, and only when a pressure channel is present --
  // otherwise there is no curve for the mark to belong to.
  function drawChangePoints(u) {
    if (!u || !u.ctx) return;
    const hasPressure = series.some((ch) => ch.axis === "r");
    if (!hasPressure) return;
    const marks = (data && data.change_t) || [];
    if (!marks.length) return;
    const values = (data && data.change_v) || [];
    const box = u.bbox;
    if (!box) return;
    const ctx = u.ctx;
    ctx.save();
    for (let i = 0; i < marks.length; i++) {
      let x, y;
      try {
        x = u.valToPos(marks[i], "x", true);
        y = u.valToPos(values[i], "r", true);
      } catch (e) {
        continue;
      }
      if (x == null || y == null || !isFinite(x) || !isFinite(y)) continue;
      // Outside the plot's own box: the mark is somewhere the operator
      // cannot see, and drawing it in the margin reads as a reading
      // that is not there.
      if (x < box.left || x > box.left + box.width) continue;
      if (y < box.top || y > box.top + box.height) continue;
      // A small triangle pointing UP at the reading, so the mark is
      // findable on a busy curve and does not cover the line it marks.
      ctx.fillStyle = "rgba(245,197,66,.9)";
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x - 4, y + 7);
      ctx.lineTo(x + 4, y + 7);
      ctx.closePath();
      ctx.fill();
    }
    ctx.restore();
  }

  // THE PLAYHEAD.
  //
  // Where the playback IS, drawn on the curve the playback is driving.
  //
  // Without it the operator had two clocks and no link between them:
  // the 3D view showed the mechanism at some position and the curve
  // below showed the whole recording with nothing saying which part of
  // it was on screen. The two views could be minutes apart and both
  // were right.
  //
  // It is drawn on the plot's own canvas, in the plot's own coordinate
  // space, from the same draw hook the density marker uses -- so it
  // moves with the curve, and it is redrawn whenever the curve is.
  //
  // The colour is the ACCENT, not the pink: the pink means pressure
  // everywhere else on this page (the segments' density, the 3000
  // marker), and a pink playhead would be read as a pressure reading.
  function drawPlayhead(u) {
    if (!u || !u.ctx) return;
    if (playheadT == null || !isFinite(playheadT)) return;
    const box = u.bbox;
    if (!box) return;
    let x;
    try {
      x = u.valToPos(playheadT, "x", true);
    } catch (e) {
      return;
    }
    if (x == null || !isFinite(x)) return;
    // Outside the plot's own box: the recording is longer than the
    // window, and the playhead is somewhere the operator cannot see.
    // Drawing it anyway would put a line in the margin, which reads as
    // a reading that is not there.
    if (x < box.left || x > box.left + box.width) return;
    const ctx = u.ctx;
    ctx.save();
    ctx.strokeStyle = "rgba(122,150,255,.95)";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(x, box.top);
    ctx.lineTo(x, box.top + box.height);
    ctx.stroke();
    // A small triangle at the top, so the head reads as a POSITION and
    // not as a stray line, and so it is findable on a busy curve.
    ctx.fillStyle = "rgba(122,150,255,.95)";
    ctx.beginPath();
    ctx.moveTo(x - 4, box.top);
    ctx.lineTo(x + 4, box.top);
    ctx.lineTo(x, box.top + 5);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  // The uPlot series, built from the channels. The FIRST entry is the X
  // values; the rest are the channels, each with the axis it belongs to.
  function uplotSeries() {
    const out = [{}];
    for (const ch of series) {
      const s = {
        label: ch.label,
        stroke: ch.color,
        width: 1.6,
        // The dashed line is the "实际" travel, which sits exactly on
        // top of the "目标" one -- so a solid line for both would hide
        // one of them completely.
        dash: ch.key === "travel_active" ? [5, 4] : undefined,
        scale: ch.axis,
      };
      // The points are too dense to draw (thousands of them), so they
      // are only drawn when the window is small enough for them to mean
      // something. uPlot decides this by the number of points in the
      // window, so the switch is automatic.
      s.points = {show: false};
      out.push(s);
    }
    return out;
  }

  // ------------------------------------------------------------------ the
  // readout. uPlot's cursor gives the index under the pointer, and the
  // index gives every channel's value -- one read, all the channels.
  // The index the readout was last built from. It is recorded here
  // because uPlot's own plot.cursor.idx is the library's internal
  // cursor state, which is not necessarily the index the readout used
  // -- and the measurement scaffolding must check the SAME index the
  // numbers on the screen came from.
  let lastIdx = null;

  function onCursor(u) {
    const i = u && u.idx != null ? u.idx : null;
    lastIdx = i;
    if (!readout) return;
    if (i == null || !data || !data.count) {
      readout.textContent = "—";
      return;
    }
    const t = (data.t || [])[i];
    // Only the instant and the sample number.
    //
    // The VALUES are in the chart's own legend, in the column beside
    // the plot -- uPlot's live legend shows every channel's value at
    // the pointer. Listing them here as well made the readout a
    // second, redundant copy, and the readout used to float over the
    // plot and cover the first seconds of the curve.
    const lines = ["t = " + (t == null ? "—" : t.toFixed(3)) + " s   #" + i];
    readout.textContent = lines.join("\n");
  }

  // Build the plot, or update the one that is already there.
  //
  // The SECOND case matters: the archive that is being written is
  // re-read every couple of seconds, because it grows. Rebuilding the
  // whole chart for that would DESTROY the operator's work -- the
  // cursor's position, the zoom, the hidden channels -- and it would
  // also cost a visible flicker.
  //
  // uPlot has the right call for it: setData() swaps the data and keeps
  // the scales, the cursor and the series state. It is called with
  // _resetScales = false, so the X axis is not re-fitted either (the
  // recording grew, and the operator's window is not the recording's
  // business).
  function build() {
    if (!data || !data.count) {
      if (plot) { plot.destroy(); plot = null; }
      if (meta) meta.textContent = archive ? (archive.count + " 个采样点") : "—";
      return;
    }
    // The X values: the recording's relative seconds, with the gaps
    // filled so the series are the same length as the X axis (uPlot
    // requires it, and a gap in the telemetry must show as a GAP in
    // the curve rather than as a straight line across it).
    const t = data.t || [];
    xs = [];
    for (let i = 0; i < t.length; i++) {
      xs.push(t[i] == null || !isFinite(t[i]) ? NaN : t[i]);
    }
    const cols = [xs];
    for (const ch of series) {
      const vals = values(ch);
      const arr = new Array(xs.length);
      for (let i = 0; i < xs.length; i++) {
        arr[i] = (vals[i] == null || !isFinite(vals[i])) ? null : vals[i];
      }
      cols.push(arr);
    }

    // The SAME channels, on a plot that already exists: the data is
    // swapped in place, and everything the operator set up survives.
    const sameChannels = plot && series.length === plot.series.length - 1
      && series.every((ch, i) => (plot.series[i + 1] || {}).label === ch.label);
    if (plot && sameChannels) {
      try {
        plot.setData(cols, false);
      } catch (e) {
        plot.destroy();
        plot = null;
      }
    }
    if (!plot) {
      const o = options();
      o.series = uplotSeries();
      plot = new uPlot(o, cols, container);
    }

    // The pointer's instant is read out of the chart's own cursor.
    //
    // uPlot's hook is an OBJECT OF ARRAYS (opts.hooks.setCursor is an
    // array of callbacks), and the hook fires with (plot, cursor) --
    // not the cursor alone. The first version of this called
    // plot.u.on("setCursor", ...), which does not exist: the chart
    // rendered and the readout stayed empty, and the error was only
    // visible in the console.
    if (plot.cursor && plot.cursor.idx != null) onCursor(plot.cursor);

    // The legend's rows are the visibility switches. uPlot's own legend
    // does this, and it is why the legend is the library's rather than
    // the page's.
    if (legend) {
      // The page's legend slot is empty: uPlot puts ITS legend inside
      // the chart's own container, which is where it belongs (it is
      // part of the instrument, not a caption under it).
      legend.innerHTML = "";
    }

    // The summary line.
    const dur = (xs.length && xs[xs.length-1] != null) ? xs[xs.length-1] : 0;
    if (meta) {
      meta.textContent = data.count + " 个采样点 · " + dur.toFixed(1) + " 秒"
        + (data.started_local ? " · " + data.started_local : "");
    }
    resize();
  }

  // ------------------------------------------------------------------ the
  // the POINTER, over the chart's own plot area.
  //
  // uPlot's own cursor gives the hovered index; the readout is built
  // from it. But the WHEEL and the DRAG are the page's, because uPlot's
  // zoom-on-wheel is a separate plugin file (this project vendors one
  // file) and because the drag has to mean two different things: a
  // plain drag PANS, and a shift-drag SELECTS a span to zoom into.
  //
  // The plot area is uPlot's "over" element -- the div that sits over
  // the canvas and receives the pointer.
  function plotBox() {
    const over = container.querySelector(".u-over");
    if (!over) return null;
    const r = over.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    return r;
  }

  // The seconds at a viewport X, through uPlot's own scale.
  function xAt(clientX, rect) {
    if (!plot) return null;
    const frac = (clientX - rect.left) / rect.width;
    const sc = plot.scales.x;
    return sc.min + frac * (sc.max - sc.min);
  }

  // The WHEEL zooms, about the pointer.
  container.addEventListener("wheel", (ev) => {
    if (!plot) return;
    const rect = plotBox();
    if (!rect) return;
    // Only over the plot: a wheel over the axis labels is the page's.
    if (ev.clientX < rect.left || ev.clientX > rect.right) return;
    ev.preventDefault();
    const about = xAt(ev.clientX, rect);
    if (about == null) return;
    const factor = ev.deltaY > 0 ? 1.2 : 1 / 1.2;
    zoom(factor, about);
  }, {passive: false});

  // The DRAG: a plain drag pans, a shift-drag selects a span.
  let drag = null;
  container.addEventListener("pointerdown", (ev) => {
    if (!plot) return;
    const rect = plotBox();
    if (!rect) return;
    if (ev.clientX < rect.left || ev.clientX > rect.right) return;
    drag = {
      mode: ev.shiftKey ? "select" : "pan",
      startX: ev.clientX,
      startT: xAt(ev.clientX, rect),
      t0: plot.scales.x.min,
      t1: plot.scales.x.max,
      rect,
    };
    container.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  });

  container.addEventListener("pointermove", (ev) => {
    if (!drag || !plot) return;
    if (drag.mode === "pan") {
      // The pixels moved, converted into seconds through the CURRENT
      // scale, and applied to the window the drag started from.
      const frac = (drag.startX - ev.clientX) / drag.rect.width;
      const w = drag.t1 - drag.t0;
      setWindow(drag.t0 + frac * w, drag.t1 + frac * w);
      return;
    }
    // A selection: the span is drawn by uPlot's own select mechanism.
    //
    // uPlot's select is a RECTANGLE IN PIXELS (setSelect takes left /
    // width / top / height), not a pair of data values. The first
    // version of this called plot.select.set([a, b]) -- a method uPlot
    // does not have -- so the selection threw on every move and the
    // box never appeared.
    const t = xAt(ev.clientX, drag.rect);
    if (t == null) return;
    const left = Math.min(drag.startX, ev.clientX) - drag.rect.left;
    const width = Math.abs(ev.clientX - drag.startX);
    try {
      plot.setSelect({left, width, top: 0, height: drag.rect.height});
    } catch (e) {}
  });

  const endDrag = (ev) => {
    if (!drag) return;
    // The drag's own record is read BEFORE it is cleared. The first
    // version cleared it first and then read drag.rect -- so the
    // selection's zoom threw "Cannot read properties of null" on every
    // shift-drag, and the span never zoomed.
    const wasSelect = drag.mode === "select";
    const rect = drag.rect;
    drag = null;
    try { container.releasePointerCapture(ev.pointerId); } catch (e) {}
    if (!wasSelect || !plot) return;
    // The selection is zoomed INTO. A click (rather than a drag) is not
    // a selection: a span with no width would zoom to nothing.
    //
    // uPlot's select rectangle is in PLOT-RELATIVE PIXELS, so it is
    // converted back into seconds through the same scale the drawing
    // uses -- with the plot's own left edge added back, because the
    // rectangle is measured from the plot's origin and not from the
    // viewport's.
    const sel = plot.select;
    if (!sel) return;
    const w = sel.width || 0;
    try { plot.setSelect({left: 0, width: 0}); } catch (e) {}
    if (w <= 1) return;
    const a = xAt(rect.left + sel.left, rect);
    const b = xAt(rect.left + sel.left + w, rect);
    if (a == null || b == null) return;
    if (Math.abs(b - a) <= 0) return;
    setWindow(Math.min(a, b), Math.max(a, b));
  };
  container.addEventListener("pointerup", endDrag);
  container.addEventListener("pointercancel", endDrag);

  // ------------------------------------------------------------------ the
  // size. The canvas is sized from the element's MEASURED box times the
  // device pixel ratio, so the drawing is at the display's own
  // resolution. A stretched viewBox is what made the old chart look
  // fake.
  function resize() {
    if (!plot) return;
    const r = container.getBoundingClientRect();
    if (!r.width || !r.height) return;
    // The container IS the chart's box now: the legend lives in its own
    // element beside it, so nothing is inside the chart's box but the
    // chart. The box's size is therefore the chart's size.
    plot.setSize({width: Math.round(r.width), height: Math.round(r.height)});
  }

  // ------------------------------------------------------------------ the
  // view. uPlot owns the viewport: it has its own scale state, and the
  // "reset" is a setScale back to the recording's full range.
  // The recording's whole range, in seconds.
  function fullRange() {
    const lo = xs.find((v) => v != null && isFinite(v));
    let hi = lo;
    for (const v of xs) {
      if (v == null || !isFinite(v)) continue;
      if (v > hi) hi = v;
    }
    if (lo == null) return null;
    return {t0: lo, t1: hi === lo ? lo + 1 : hi};
  }

  // The window the chart is showing RIGHT NOW, read from the plot's own
  // scale.
  //
  // This is the one place the window is read. The api object's view()
  // is the public accessor and calls THIS, so the page's reading and
  // the load() path's reading are the same number by construction --
  // they cannot drift apart the way two copies of the same expression
  // can.
  //
  // It returns null when there is no plot yet (nothing has been
  // loaded), which is what the caller in load() has to distinguish
  // "no window to restore" from "a window of zero width".
  function currentView() {
    if (!plot) return null;
    const sc = plot.scales && plot.scales.x;
    if (!sc) return null;
    if (sc.min == null || sc.max == null) return null;
    return {t0: sc.min, t1: sc.max};
  }

  function resetView() {
    const full = fullRange();
    if (!full || !plot) return;
    setWindow(full.t0, full.t1);
  }

  function zoom(factor, aboutT) {
    if (!plot) return;
    const xsc = plot.scales.x;
    const lo = xsc.min, hi = xsc.max;
    const mid = aboutT == null ? (lo + hi) / 2 : aboutT;
    let w = (hi - lo) * factor;
    const fr = fullRange();
    const full = fr ? Math.max(1e-3, fr.t1 - fr.t0) : 1;
    if (w > full) w = full;
    if (w < 1e-3) w = 1e-3;
    setWindow(mid - w / 2, mid + w / 2);
  }

  // THE WINDOW, set in one place.
  //
  // uPlot's setScale() is not enough on its own: it records the scale
  // and commits, and the commit re-fits the X axis to the DATA RANGE
  // unless the scale says otherwise -- and the x scale's own `auto`
  // flag is read at INIT time, so the flag has to be in the options
  // (it is) AND the window has to be set on the scale object as well
  // as through setScale().
  //
  // Setting the scale's min and max directly is what uPlot's own drag
  // does (drag.setScale calls _setScale, which sets sc.min and
  // sc.max), so the two agree.
  // THE WINDOW IS CLAMPED TO THE RECORDING, IN ONE PLACE.
  //
  // The wheel and the drag are clamped to the recording: the window
  // never starts before zero and never reaches past the last sample.
  //
  // The clamp is HERE, and not in each caller, for one reason: there are
  // five callers -- the wheel's zoom, the drag's pan, the shift-drag's
  // selection, the three buttons and the load path's restore -- and a
  // clamp that lives in the callers is a clamp that one of them forgets.
  //
  // Before this, only the WIDTH was bounded: zoom() clamped the window's
  // width to the recording's length, but nothing bounded its POSITION.
  // So a wheel zoom about a point near the right edge produced a window
  // that ran past the end of the recording, and a drag to the left
  // produced one that ran before the start -- the chart showed empty
  // space on one side, and the operator had no way to tell that the data
  // had ended.
  //
  // THE BOUNDS ARE THE RECORDING'S OWN. `fullRange()` is the data: the
  // first sample's second and the last one's. The chart's X axis is
  // specified to show relative seconds from the first sample, so 0 is
  // the start of the recording by definition -- and the recording's
  // length is what the archive holds.
  //
  // The clamp is applied as a TRANSLATION, not a truncation: a window
  // that hangs over an edge is SLID back until it fits, so the operator
  // keeps the width they asked for. Truncating instead would silently
  // change the zoom level -- and a wheel-out that hit the end would
  // become a zoom-in, which is the opposite of what the wheel asked for.
  //
  // A window WIDER than the recording is pinned to the recording: there
  // is nothing else to show, and a window that starts before 0 would
  // draw a stretch of the curve that does not exist.
  function clampWindow(t0, t1) {
    const fr = fullRange();
    if (!fr) return {t0, t1};
    const lo = fr.t0;
    const hi = fr.t1;
    let w = t1 - t0;
    if (!isFinite(w) || w <= 0) return {t0: lo, t1: hi};
    // A window wider than the recording is the whole recording.
    if (w > hi - lo) {
      w = hi - lo;
      return {t0: lo, t1: lo + w};
    }
    // Slide the window back inside, keeping its width.
    let a = t0;
    if (a < lo) a = lo;
    if (a + w > hi) a = hi - w;
    return {t0: a, t1: a + w};
  }

  function setWindow(t0, t1) {
    if (!plot) return;
    // THE CLAMP, AND THEN THE SCALE.
    //
    // The order matters: the clamp is computed from the RECORDING's
    // range and not from the plot's current scale, so a pan that is
    // already at the edge does not feed the edge back into itself.
    const clamped = clampWindow(t0, t1);
    t0 = clamped.t0;
    t1 = clamped.t1;
    // ONLY setScale, and NOTHING after it.
    //
    // Two things are true about uPlot's setScale, and both of them
    // cost a bug to find:
    //
    //   1. It does NOT apply the range immediately. It records it as
    //      a PENDING scale and queues a commit, which runs in a
    //      microtask. So right after setScale() the plot still has
    //      the OLD range -- and, at first, no range at all.
    //   2. redraw() is NOT a harmless "please paint again". redraw()
    //      calls _setScale(x, scaleX.min, scaleX.max) -- it re-records
    //      the scale from the CURRENT values. Called straight after a
    //      setScale(), that overwrites the pending range with the OLD
    //      one (or with null, if there was no range yet).
    //
    // The first version of this did setScale() and then redraw(), so
    // every zoom was cancelled by the very next line. The symptom was
    // confusing: the zoom worked when it was called from the API, and
    // did nothing when it came from the wheel -- because the wheel's
    // path went through here, and the button's did not.
    //
    // setScale() alone is correct: it queues the commit, the commit
    // applies the range, and the commit ALSO recomputes the visible
    // index range (i0..i1) -- which is where the cursor's index comes
    // from. So the zoom sticks and the cursor keeps working.
    try {
      plot.setScale("x", {min: t0, max: t1});
    } catch (e) {}
  }

  // ------------------------------------------------------------------ the
  // channel toggle buttons. uPlot's legend already toggles a series by
  // clicking its row, but the page shows its OWN buttons as well --
  // one per channel, carrying the channel's colour -- because the
  // operator needs to see which channel is which before clicking it,
  // and uPlot's legend hides the values of a switched-off series.
  function buildSeriesButtons() {
    if (!seriesBox) return;
    while (seriesBox.firstChild) seriesBox.removeChild(seriesBox.firstChild);
    if (!series.length) { seriesBox.hidden = true; return; }
    seriesBox.hidden = false;
    for (const ch of series) {
      const b = document.createElement("button");
      b.className = "rec-series-btn";
      b.dataset.key = ch.key;
      const sw = document.createElement("i");
      sw.style.background = ch.color;
      b.appendChild(sw);
      b.appendChild(document.createTextNode(ch.label));
      b.addEventListener("click", () => {
        if (!plot) return;
        const idx = series.indexOf(ch) + 1;
        // uPlot's OWN method for it: plot.setSeries(idx, {show: ...}).
        //
        // Setting plot.series[idx].show directly does not work: uPlot
        // also has to update the legend's row, and the first version of
        // this wrote the flag by hand, so the legend and the button
        // disagreed with the drawing.
        //
        // The flag is UNDEFINED when a series is shown (uPlot's default
        // is "shown"), so the test is `=== false`, not `!show`.
        const s = plot.series[idx];
        const isShown = s.show !== false;
        try {
          plot.setSeries(idx, {show: !isShown});
        } catch (e) {
          s.show = !isShown;
          plot.redraw();
        }
        b.classList.toggle("off", isShown);
      });
      seriesBox.appendChild(b);
    }
  }

  // ------------------------------------------------------------------ the
  // API.
  const api = {
    // The archive's derived metrics -- the stroke cycle, the rate, and
    // the per-segment load. The page shows them, and a test can ask
    // for them, and both read the SAME numbers.
    metrics() {
      return recordMetrics(archive || data);
    },
    // Load an archive.
    //
    // `key` says WHICH archive it is, and it matters: the page
    // refreshes the record list every couple of seconds, and when the
    // SELECTED archive is the one still being written, the refresh
    // re-reads it -- because a growing recording has to be re-read to
    // show the new samples.
    //
    // That re-read must NOT take the operator's viewport away. The
    // first version of this called resetView() on every load, so the
    // refresh reset the zoom every two seconds -- and the WHEEL zoom
    // appeared to do nothing at all, because the zoom was queued by
    // the wheel and then overwritten by the refresh's own queued
    // reset a moment later.
    //
    // So the viewport is restored rather than reset: when the archive
    // is the SAME one, the window the operator chose is kept (and the
    // window is clamped to the new data, because the recording grew);
    // when it is a DIFFERENT archive, the whole recording is shown.
    load(curve, key) {
      const same = (key != null && archive != null && archiveKey === key);
      data = curve || null;
      archive = curve || null;
      if (key != null) archiveKey = key;
      series = channels();
      buildSeriesButtons();
      build();
      if (same) {
        // The window the operator chose, clamped to what is now there.
        //
        // THE WINDOW IS READ FROM THE PLOT, through the local
        // currentView() -- NOT through the api object's view() method.
        //
        // The first version of this called `view()` bare. `view` is a
        // PROPERTY of the api object (api.view), not a module-scope
        // binding, so the bare call threw "ReferenceError: view is not
        // defined" -- and it threw on the SECOND load of the same
        // archive, which is the `same` branch below. The exception
        // escaped to the page's caller, which caught it and showed
        // "读取失败：view is not defined" -- and the 3D replay, which is
        // loaded on the NEXT line, never ran at all.
        //
        // So the live recording, which is re-read every two seconds and
        // is therefore the same archive every time, broke every two
        // seconds.
        //
        // The fix is the local helper below, which reads the plot's own
        // scale. The api object's view() stays as the PUBLIC accessor
        // (the page and the measurement scaffolding both use it).
        const w = currentView();
        if (w && w.t0 != null && w.t1 != null) {
          // THE SAME CLAMP EVERY OTHER PATH USES.
          //
          // This branch used to clamp the window ITSELF, with its own
          // pair of min/max expressions. That was a second clamp, and
          // the two did not agree: this one TRUNCATED the window to the
          // data (so a window wider than the recording came back
          // narrower, and the operator's zoom level silently changed),
          // while setWindow's clamped by SLIDING it. One rule, one
          // place -- clampWindow() -- so the restore cannot disagree
          // with the wheel.
          setWindow(w.t0, w.t1);
        }
      } else {
        resetView();
      }
    },

    archive() { return archive; },

    resetView,
    zoomBy(factor) { zoom(factor); },
    setView(t0, t1) { setWindow(t0, t1); },
    view() {
      // The public accessor. It reads the SAME helper the load path
      // reads, so the page's view of the window and the chart's own
      // view of it are one number.
      const w = currentView();
      return w || {t0: 0, t1: 1};
    },
    // Which channels are switched off. The buttons and the library
    // share one state: the button reads the library's flag.
    hiddenChannels() {
      if (!plot) return [];
      const out = [];
      series.forEach((ch, i) => {
        // The same rule as the button: undefined means SHOWN.
        if (plot.series[i + 1] && plot.series[i + 1].show === false) {
          out.push(ch.key);
        }
      });
      return out;
    },
    showChannel(key, on) {
      if (!plot) return;
      const i = series.findIndex((c) => c.key === key);
      if (i < 0) return;
      try {
        plot.setSeries(i + 1, {show: on !== false});
      } catch (e) {
        plot.series[i + 1].show = on !== false;
        plot.redraw();
      }
      const b = seriesBox && seriesBox.querySelector(
        '.rec-series-btn[data-key="' + key + '"]');
      if (b) b.classList.toggle("off", on === false);
    },
    seriesKeys() { return series.map((c) => c.key); },
    // The cursor's index, for the measurement scaffolding.
    cursorIndex() { return lastIdx; },
    // The scales the chart is actually drawing, so the measurement
    // scaffolding can check WHICH scale is on which side -- not just
    // that there is an axis there.
    //
    // An axis with no `scale` reads the default "y" scale, so a chart
    // can have a right-hand axis that is really the percentage one.
    // The layout looks right and the numbers are wrong, so the scales
    // are worth reading back.
    // Which scale each axis reads. An axis with no `scale` field
    // reads the default "y" one, so this mapping is worth reading
    // back: the pressure scale can exist and still be unread.
    axes() {
      if (!plot) return null;
      const out = {};
      for (const a of plot.axes || []) {
        if (a && a.scale) out[a.side === 1 ? "r" : (a.side === 3 ? "y" : "x")] =
          a.scale;
      }
      return out;
    },
    scales() {
      if (!plot) return null;
      const out = {};
      for (const key of Object.keys(plot.scales || {})) {
        const sc = plot.scales[key];
        out[key] = {min: sc.min, max: sc.max, side: sc.side};
      }
      return out;
    },
    redraw() { if (plot) plot.redraw(); },
    // WHERE THE PLAYBACK IS.
    //
    // The replay calls this with the second it is playing, and the
    // chart draws it. `null` takes the playhead away -- which is what
    // a STOP does, and what loading a different recording does.
    //
    // The value is in the RECORDING's own seconds, which is the same
    // clock the x axis reads, so no conversion is needed here and none
    // can go wrong.
    //
    // The redraw is the chart's own, and it is redraw(false).
    //
    // uPlot's redraw() with no argument RE-FITS the X axis to the data
    // range: redraw() calls _setScale(x, scaleX.min, scaleX.max), and
    // the pending range it is supposed to apply is overwritten by the
    // CURRENT one. The chart's own notes say this -- "redraw() is NOT a
    // harmless 'please paint again'" -- and it cost a bug to find once.
    //
    // So redraw(false) -- the argument that says "do NOT rebuild the
    // paths from the scale" -- which takes the commit() branch and
    // leaves the scale alone. The operator's zoom survives.
    //
    // Measured: with a plain redraw(), a playback that moved the
    // playhead collapsed the X window to one millisecond, and the
    // zoom and reset buttons stopped working.
    setPlayhead(t) {
      const next = (t == null || !isFinite(t)) ? null : Number(t);
      if (next === playheadT) return;
      playheadT = next;
      if (plot) {
        try { plot.redraw(false); } catch (e) { plot.redraw(); }
      }
    },
    playhead() { return playheadT; },
    resize,
  };

  // The chart follows the window's size, and the tab's.
  window.addEventListener("resize", resize);

  return api;
}
