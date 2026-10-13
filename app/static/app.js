"use strict";

// No CDN dependency; SVG charts use only textContent and fixed element names.
const number = value => value == null ? "N/A" : new Intl.NumberFormat("en").format(value);
const timestamp = value => new Date(value).toISOString().replace("T", " ").slice(0, 16) + " UTC";
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function svgNode(tag, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  return el;
}
async function renderTrend(container) {
  try {
    const response = await fetch(container.dataset.trend);
    if (!response.ok) throw new Error("unavailable");
    const points = await response.json();
    container.replaceChildren();
    const known = points.filter(p => p.value !== null);
    if (!known.length) {
      container.append(node("p", "No observations available yet. History builds with each collection.", "chart-empty muted"));
      return;
    }
    container.append(node("p", `${container.dataset.label}: ${number(known[0].value)} → ${number(known.at(-1).value)} · ${known.length} observations`, "chart-description"));
    if (known.length < 2) {
      container.append(node("p", "First observation recorded. A trend will appear after the next collection.", "chart-empty muted"));
      return;
    }
    const low = Math.min(...known.map(p => p.value));
    const high = Math.max(...known.map(p => p.value));
    const first = +new Date(points[0].time), last = +new Date(points.at(-1).time);
    const svg = svgNode("svg", {viewBox: "0 0 900 210", role: "img", "aria-label": `${container.dataset.label} over time. Range ${number(low)} to ${number(high)}.`});
    for (let i = 0; i < 4; i++) {
      const y = 15 + i * 56;
      svg.append(svgNode("line", {x1: 75, x2: 880, y1: y, y2: y, stroke: "#e8eeed", "stroke-dasharray": "4 4"}));
      const label = svgNode("text", {x: 0, y: y + 4, fill: "#87968d", "font-size": 11});
      label.textContent = number(Math.round(high - (high - low) * i / 3));
      svg.append(label);
    }
    let path = "", broken = true;
    for (const point of points) {
      if (point.value === null) { broken = true; continue; }
      const x = 75 + (+new Date(point.time) - first) / Math.max(last - first, 1) * 805;
      const y = high === low ? 99 : 183 - (point.value - low) / (high - low) * 168;
      path += `${broken ? "M" : "L"}${x},${y} `;
      broken = false;
      const circle = svgNode("circle", {cx: x, cy: y, r: 3, fill: "#278773"});
      const title = svgNode("title", {});
      title.textContent = `${timestamp(point.time)}: ${number(point.value)}`;
      circle.append(title); svg.append(circle);
    }
    svg.append(svgNode("path", {d: path, fill: "none", stroke: "#278773", "stroke-width": 2}));
    const labels = node("div", undefined, "chart-labels");
    labels.append(node("span", timestamp(points[0].time)), node("span", timestamp(points.at(-1).time)));
    container.append(svg, labels);
  } catch {
    container.replaceChildren(node("p", "Trend unavailable. Please reload to try again.", "chart-empty muted"));
  }
}
document.querySelectorAll("[data-trend]").forEach(renderTrend);
document.querySelector("#add-channel")?.addEventListener("submit", event => {
  const button = event.currentTarget.querySelector("button");
  button.disabled = true; button.textContent = "Adding…";
});
document.querySelector("#digest-button")?.addEventListener("click", async event => {
  const button = event.currentTarget, status = document.querySelector("#digest-status");
  button.disabled = true; status.textContent = "Reading collected posts and generating a digest…";
  try {
    const response = await fetch(button.dataset.url, {method: "POST"});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "AI unavailable. Please try later.");
    document.querySelector("#digest-content").textContent = result.content;
    status.textContent = `${timestamp(result.created_at)} · ${result.source_count} source posts · ${result.model}`;
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
});
const poll = document.querySelector("#poll-state");
if (poll) {
  const previous = JSON.stringify(JSON.parse(poll.dataset.initial));
  setInterval(async () => {
    if (document.hidden || document.querySelector("#digest-button:disabled") || document.activeElement?.tagName === "INPUT") return;
    try {
      const response = await fetch("/api/channels");
      if (!response.ok) return;
      let rows = await response.json();
      if (poll.dataset.channel) rows = rows.filter(row => String(row.id) === poll.dataset.channel);
      const current = JSON.stringify(rows.map(row => row.version));
      if (previous !== current) window.location.reload();
    } catch { /* Keep existing data visible during a transient connection failure. */ }
  }, 4000);
}
