export function shapesPlugin({ getState, kind }) {
  return {
    hooks: {
      draw: [(u) => {
        const st = getState();
        const ctx = u.ctx;
        const { left, top, width, height } = u.bbox;
        ctx.save();
        const vline = (sec, color, dash) => {
          if (sec == null) return;
          const x = Math.round(u.valToPos(sec, "x", true));
          if (x < left || x > left + width) return;
          ctx.beginPath(); ctx.setLineDash(dash || []); ctx.strokeStyle = color;
          ctx.lineWidth = Math.max(1, Math.round(devicePixelRatio));
          ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
        };
        const hline = (val, color, dash) => {
          const y = Math.round(u.valToPos(val, u.series[1]?.scale || "y", true));
          ctx.beginPath(); ctx.setLineDash(dash || []); ctx.strokeStyle = color;
          ctx.lineWidth = 1; ctx.moveTo(left, y); ctx.lineTo(left + width, y); ctx.stroke();
        };
        if (kind === "price") {
          for (const r of st.regions || []) {
            const x0 = Math.round(u.valToPos(r.x0, "x", true));
            const x1 = Math.round(u.valToPos(r.x1, "x", true));
            ctx.fillStyle = r.kind === "charge" ? "rgba(210,153,34,0.10)" : "rgba(63,185,80,0.10)";
            ctx.fillRect(x0, top, Math.max(1, x1 - x0), height);
          }
          if (st.thresholdC != null) hline(st.thresholdC, "rgba(139,148,158,0.35)", [2, 3]);
        }
        if (kind === "soc" && st.socFloorPct != null) hline(st.socFloorPct, "#f85149", [4, 3]);
        if (kind === "grid" || kind === "cost") hline(0, "#444c56", []);
        vline(st.nowSec, "#56d364", [2, 4]);
        vline(st.cursorSec, "#58a6ff", []);
        ctx.restore();
      }],
    },
  };
}
