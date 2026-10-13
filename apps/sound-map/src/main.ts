// Whale and Dolphin Orchestra: seat recordings from the map, compose them through the Python engine
// (ACE-Step on the combined guide), then listen while each site pulses on its own calls.

import { Composer } from "./composer.ts";
import { instrumentIcon } from "./instruments.ts";
import { SPECIES } from "./lib/species.ts";
import { renderIndividualTracks, renderLanes, renderScores, renderSelectedTracks, partColor } from "./lanes.ts";
import { catalogUrl, comboUrl, combosUrl, fileUrl, isStatic } from "./lib/api.ts";
import { activePulses, soundingParts } from "./lib/pulses.ts";
import { earlierPieces, precomputedPieces, specKey } from "./lib/precomputed.ts";
import { readComposition } from "./lib/combine-reader.ts";
import { bundledModels, isModelRegistry, modelLabel, modelMatches, modelSatisfies, renderedModel } from "./lib/model-variant.ts";
import { initialCatalog, isCatalog, sameCatalog, saveCatalog } from "./lib/catalog-snapshot.ts";
import worldCountries from "../../../resources/maps/world-countries-110m.geojson" with { type: "json" };
import { CATEGORY, THREATENED, TREND_ARROW, TREND_WORD, statusLine, statusOf, voiceInfo, type Status } from "./lib/status.ts";
import type { Catalog, CompositionJob, CompositionProgress, ComboSummary, GenerationRuntime, Manifest, ModelVariant, PrecomputedIndex, Source, Spec, Species, StemName } from "./lib/types.ts";
import { AtlasMap, SPECIES_COLOR, sitesOf, type Countries, type MapPart, type Site } from "./map.ts";
import { Player, type Track } from "./player.ts";
import { setSilverViewerActive } from "./silver-viewer.ts";

const $ = <T extends Element>(selector: string) => document.querySelector(selector) as T;
const esc = (value: unknown) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);
const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);
const statusChip = (status: Status) => `<i class="status-chip ${status.category.toLowerCase()}" title="${esc(CATEGORY[status.category])}">${status.category}</i>`;
/** The status line under a recording: chip, then who, listing, count and trend, with a button to the voice card. */
const statusRow = (source: Source) => {
  const status = statusOf(source);
  return `<span class="rec-status">${statusChip(status)} <span>${esc(statusLine(status))}</span> <button class="more" type="button" data-info="${esc(source.id)}" aria-label="More about ${esc(status.group)}">More</button></span>`;
};
const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const PULSE_LIFETIME = 0.9;
const STEM_LABELS: Record<StemName, string> = { animal: "Recorded voices", guide: "Musical guide", response: "Constructed response", ace: "Generated music" };
let result: Manifest | null = null;
$<HTMLImageElement>("#hall-image").src = fileUrl("concert-hall-atlas-v1.png");

/* ---------- listening programme beside the map ---------- */
const programmeToggle = $<HTMLButtonElement>("#programme-toggle");
function setProgrammeOpen(open: boolean) {
  programmeToggle.setAttribute("aria-expanded", String(open));
  programmeToggle.title = open ? "Collapse the listening programme" : "Expand the listening programme";
  $("#programme-panel").toggleAttribute("hidden", !open);
  $("#map-workspace").classList.toggle("programme-collapsed", !open);
}
setProgrammeOpen(true);
programmeToggle.addEventListener("click", () => setProgrammeOpen(programmeToggle.getAttribute("aria-expanded") !== "true"));
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && programmeToggle.getAttribute("aria-expanded") === "true" && document.activeElement?.closest(".programme-drawer")) {
    setProgrammeOpen(false);
    programmeToggle.focus();
  }
});

/* ---------- accessible project views ---------- */
type View = "concert" | "technical" | "installation" | "silver" | "about";
const VIEWS: View[] = ["concert", "technical", "installation", "silver", "about"];
// Server-only tabs (Sound to silver) are hidden in the static copy, so keys and URLs never land on them there.
const viewTabs = [...document.querySelectorAll<HTMLButtonElement>("[role=tab][data-view]")]
  .filter((tab) => !(isStatic && tab.hasAttribute("data-server-only")));
// About and Installation photos live with the demo media; lazy loading waits until their panel is shown.
for (const img of document.querySelectorAll<HTMLImageElement>("img[data-file]")) img.src = fileUrl(img.dataset.file!);
if (!isStatic) for (const img of document.querySelectorAll<HTMLImageElement>("img[data-silver-file]")) {
  img.src = `/silver/files/${img.dataset.silverFile!}`;
}
const contextChoices = [...document.querySelectorAll<HTMLButtonElement>("[data-context-choice]")];
const contextPanels = [...document.querySelectorAll<HTMLElement>("[data-context-panel]")];
for (const choice of contextChoices) choice.addEventListener("click", () => {
  const selected = choice.dataset.contextChoice;
  for (const option of contextChoices) option.setAttribute("aria-pressed", String(option === choice));
  for (const panel of contextPanels) panel.toggleAttribute("hidden", panel.dataset.contextPanel !== selected);
});
// Links to server routes (such as /silver) have no target in the static copy.
if (isStatic) for (const link of document.querySelectorAll<HTMLElement>("[data-server-only]")) link.hidden = true;
function showView(view: View, scroll = false, sectionId?: string) {
  for (const tab of viewTabs) {
    const active = tab.dataset.view === view;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  }
  for (const name of VIEWS) $(`#${name}-panel`).toggleAttribute("hidden", view !== name);
  if (!isStatic) setSilverViewerActive(view === "silver");
  const panel = $<HTMLElement>(`#${view}-panel`);
  const section = sectionId ? document.getElementById(sectionId) : null;
  const destination = section && panel.contains(section) ? section : panel;
  const url = new URL(location.href);
  if (view === "concert") url.searchParams.delete("view");
  else url.searchParams.set("view", view);
  if (destination === section) url.hash = section!.id;
  history.replaceState(null, "", url);
  if (scroll) {
    destination.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
    $<HTMLButtonElement>(`#${view}-tab`).focus({ preventScroll: true });
  }
}
for (const tab of viewTabs) tab.addEventListener("click", () => showView(tab.dataset.view as View));
for (const tab of viewTabs) tab.addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const index = event.key === "Home" ? 0 : event.key === "End" ? viewTabs.length - 1 : (viewTabs.indexOf(tab) + (event.key === "ArrowRight" ? 1 : viewTabs.length - 1)) % viewTabs.length;
  const next = viewTabs[index]!;
  next.focus();
  showView(next.dataset.view as View);
});
document.addEventListener("click", (event) => {
  const target = (event.target as Element).closest<HTMLElement>("[data-show-technical], [data-show-concert], [data-show-view]");
  if (!target) return;
  event.preventDefault();
  showView((target.dataset.showView as View | undefined) ?? (target.hasAttribute("data-show-technical") ? "technical" : "concert"), true, target.dataset.viewSection);
});
const requestedView = new URL(location.href).searchParams.get("view") as View | null;
const reachable = (view: View | null): view is View => Boolean(view && viewTabs.some((tab) => tab.dataset.view === view));
showView(reachable(requestedView) ? requestedView : "concert");

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error ?? `${url}: ${response.status}`);
  return response.json() as Promise<T>;
}

let generationRuntime: GenerationRuntime | null = null;
let runtimeFailed = false;
let catalogUnavailable = false;
let catalogUpdate: Catalog | null = null;
let catalogEtag: string | null = null;
let dockReady = false;
function showEngineNotice() {
  const note = $<HTMLElement>("#engine-notice");
  const unavailable = runtimeFailed || catalogUnavailable;
  const message = isStatic && catalogUnavailable
    ? "The recording catalog could not be loaded. The map shows the bundled recording locations. Reload when the files are available."
    : isStatic
    ? "Playback only: the Python composition engine is not connected. Recordings and saved pieces remain playable."
    : catalogUnavailable
    ? "The recording server cannot be reached. The map shows the bundled recording locations. Audio and new compositions need the server."
    : runtimeFailed
    ? "The composition service cannot be reached. The map remains available; existing audio files can still play if the file server is reachable."
    : generationRuntime?.python_available === false
    ? "The Python composition engine is unavailable. You can still listen to recordings and saved pieces."
    : generationRuntime?.ace_available === false
    ? "The music model is unavailable. Recorded voices, saved pieces, musical guides and constructed responses remain available."
    : "";
  $<HTMLElement>("#engine-message").textContent = [message, catalogUpdate ? "Updated recording details are available." : ""].filter(Boolean).join(" ");
  $<HTMLButtonElement>("#connection-retry").hidden = isStatic || !unavailable;
  $<HTMLButtonElement>("#catalog-update").hidden = !catalogUpdate;
  note.hidden = !message && !catalogUpdate;
  note.classList.toggle("disconnected", unavailable);
}

async function refreshCatalog() {
  try {
    const response = await fetch(catalogUrl(), { headers: catalogEtag ? { "if-none-match": catalogEtag } : {}, signal: AbortSignal.timeout(10_000) });
    if (response.status !== 304) {
      if (!response.ok) throw new Error("The recording catalog is unavailable.");
      const update: unknown = await response.json();
      if (!isCatalog(update)) throw new Error("The recording catalog could not be read.");
      catalogUpdate = sameCatalog(catalog, update) ? null : update;
      catalogEtag = response.headers.get("etag");
    }
    catalogUnavailable = false;
  } catch {
    catalogUnavailable = true;
  }
  showEngineNotice();
  if (dockReady) renderDock();
}

$<HTMLButtonElement>("#connection-retry").addEventListener("click", () => {
  void refreshCatalog();
  void refreshRuntime();
  void loadSaved();
});
$<HTMLButtonElement>("#catalog-update").addEventListener("click", () => {
  if (!catalogUpdate) return;
  try {
    saveCatalog(sessionStorage, catalogUpdate);
    if (composer?.count) sessionStorage.setItem("whales-catalog-draft", JSON.stringify(composer.spec()));
    location.reload();
  } catch {
    playbackError("The browser could not save the updated recording catalog. Please allow browser storage and try again.");
  }
});
async function refreshRuntime() {
  try {
    generationRuntime = await json<GenerationRuntime>("/api/runtime", { signal: AbortSignal.timeout(10_000) });
    runtimeFailed = false;
  } catch {
    runtimeFailed = true;
  }
  showEngineNotice();
  if (dockReady) renderDock();
}
showEngineNotice();
if (!isStatic) {
  void refreshRuntime();
  setInterval(() => { if (document.visibilityState === "visible") void refreshRuntime(); }, 30_000);
}

const catalog = (() => { try { return initialCatalog(sessionStorage); } catch { return initialCatalog(); } })();
const countries: Countries = worldCountries;
const sites = sitesOf(catalog.sources);
const sourceById = new Map(catalog.sources.map((s) => [s.id, s]));
const siteOf = new Map(sites.flatMap((site) => site.sources.map((s) => [s.id, site] as const)));
const player = new Player();
const preview = new Audio();
let pieceKey: string | null = null; // the orchestra spec the piece on screen was made from
const rendered = new Map<string, string>(); // precomputed ensembles, plus pieces composed or opened this session
const renderedModels = new Map<string, ModelVariant | null>();
const previousVersions = new Map<string, string>();
let modelRegistry = bundledModels;
let selectedModel: ModelVariant = modelRegistry.default_model;
try { const saved = sessionStorage.getItem("whales-compose-model"); if (modelRegistry.models.some(m => m.id === saved)) selectedModel = saved!; } catch { /* Use the configured default. */ }
let modelChoiceRevision = 0;
function renderModelChoices() {
  const select = $<HTMLSelectElement>("#compose-model");
  const options = (recommended: boolean) => modelRegistry.models.filter(m => Boolean(m.recommended) === recommended).map(m =>
    `<option value="${esc(m.id)}">${esc(m.label)}${m.characteristics.length ? ` · ${esc(m.characteristics.join(", "))}` : ""}${m.available === false ? " · unavailable" : ""}</option>`).join("");
  select.innerHTML = (options(true) ? `<optgroup label="Recommended">${options(true)}</optgroup>` : "")
    + (options(false) ? `<optgroup label="Other models">${options(false)}</optgroup>` : "");
  select.value = selectedModel;
}
async function refreshModels() {
  if (isStatic) return;
  const registry = await json<unknown>("/api/models", { signal: AbortSignal.timeout(15_000) }).catch(() => null);
  if (!isModelRegistry(registry)) return;
  modelRegistry = registry;
  if (!registry.models.some(m => m.id === selectedModel)) selectedModel = registry.default_model;
  renderModelChoices();
  if (dockReady) renderDock();
}
renderModelChoices();
const precomputedIds = new Set<string>();
async function loadPrecomputed() {
  const indices = await Promise.all(["duos.json", "trios.json"].map(path =>
    json<PrecomputedIndex>(fileUrl(path), { signal: AbortSignal.timeout(10_000) }).catch(() => null)));
  for (const index of indices) {
    for (const [key, id] of precomputedPieces(index, catalog)) { rendered.set(key, id); renderedModels.set(key, "base"); precomputedIds.add(id); }
    for (const [key, id] of earlierPieces(index, catalog)) previousVersions.set(key, id);
  }
  if (dockReady) renderDock();
  if (summaries.size) void loadSaved();
}
let busy = false;
let compositionJob: CompositionJob | null = null;
let compositionProgress: CompositionProgress | null = null;
let compositionError = "";
const JOB_STORAGE = "whales-composition-job";
const rememberJob = (id: string | null) => {
  try {
    if (id) sessionStorage.setItem(JOB_STORAGE, id);
    else sessionStorage.removeItem(JOB_STORAGE);
  } catch { /* Composition still works when browser storage is disabled. */ }
};
let flash = "";
let flashTimer = 0;
let openSiteKey: string | null = null;
const stemOn: Record<StemName, boolean> = { animal: true, guide: false, response: false, ace: true };
let partOn: boolean[] = [];
let setPlayhead: (t: number | null) => void = () => {};
let setIndividualPlayhead: (t: number | null) => void = () => {};
let frame = 0;
let setTechnicalPlayhead: (t: number | null) => void = () => {};

const technicalSource = $<HTMLSelectElement>("#technical-source");
technicalSource.innerHTML = catalog.sources.map((source) => `<option value="${esc(source.id)}">${esc(source.title)} · ${esc(capital(source.species))}</option>`).join("");
function renderMeasurement(source: Source) {
  preview.pause();
  resetPreview();
  const ticks = source.onsets_s.map((t) => `<line x1="${(t / source.duration_s * 1000).toFixed(1)}" x2="${(t / source.duration_s * 1000).toFixed(1)}" y1="0" y2="12" stroke="${SPECIES_COLOR[source.species]}" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`).join("");
  const overlay = (band: [number, number], shift: number) => ticks + source.events.map((event) => {
    if (event.k !== "t") return "";
    const points = event.c.map(([t, hz]) => {
      const y = Math.max(0, Math.min(100, 100 * (1 - Math.log(hz * 2 ** shift / band[0]) / Math.log(band[1] / band[0]))));
      return `${(t / source.duration_s * 1000).toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    return `<polyline points="${points}" fill="none" stroke="${SPECIES_COLOR[source.species]}" stroke-width="1.6" vector-effect="non-scaling-stroke"/>`;
  }).join("");
  const card = (stem: "animal" | "guide", label: string, title: string) => {
    const file = source.files[stem];
    return `<article class="example-card"><div class="example-head"><div><span class="section-number">${label}</span><h3>${title}</h3></div><button class="btn" type="button" data-example-listen="${esc(source.id)}" data-stem="${stem}" data-label="▶ Listen" aria-label="Listen to ${stem === "animal" ? "recording" : "musical guide"}: ${esc(source.title)}" aria-pressed="false">▶ Listen</button></div><div class="example-spec"><img src="${esc(fileUrl(file.image))}" alt="${stem === "animal" ? "Recording" : "Musical guide"} spectrogram for ${esc(source.title)}"><svg viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true">${overlay(file.band, stem === "guide" ? source.register_shift_octaves : 0)}</svg></div><div class="spec-axis"><span>0 s → ${source.duration_s.toFixed(0)} s</span><span>${file.band[0]}–${file.band[1]} Hz · log scale</span></div></article>`;
  };
  $("#measurement-example").innerHTML = `<div class="example-grid">${card("animal", "Input / Recorded audio", "The animal's voice")}${card("guide", "Guide / Constructed audio", "The measured phrase, played")}</div><div class="measurement-meta"><span><span class="small">Recording site</span><b>${esc(source.location.label)}</b></span><span><span class="small">Measured material</span><b>${source.onsets_s.length} ${source.material === "clicks" ? "click onsets" : "call onsets"}</b></span><span><span class="small">Guide instrument</span><b>${instrumentIcon(source.instrument)} ${esc(source.instrument)}</b></span><span><span class="small">Register shift</span><b>${source.register_shift_octaves} octaves</b></span></div><p class="measurement-note">${esc(source.note)} Coloured ticks mark measured onsets${source.material === "tonal" ? "; lines trace measured pitch contours" : "; click groups become sequences of mallet strikes"}. The guide changes the sound while preserving those event times.</p>`;
}
technicalSource.addEventListener("change", () => {
  const source = sourceById.get(technicalSource.value);
  if (source) renderMeasurement(source);
});
if (catalog.sources[0]) renderMeasurement(catalog.sources[0]);
$("#measurement-example").addEventListener("click", (event) => {
  const button = (event.target as Element).closest<HTMLButtonElement>("[data-example-listen]");
  const source = button && sourceById.get(button.dataset.exampleListen ?? "");
  if (button && source) listen(source, button, button.dataset.stem === "guide" ? "guide" : "animal");
});
$<HTMLElement>("#sources-count").textContent = String(catalog.sources.length);
$<HTMLElement>("#catalog-summary").textContent = `${catalog.sources.length} recordings / ${sites.length} sites / ${new Set(catalog.sources.map((s) => s.species)).size} species`;
async function hearPiece() {
  const first = [...summaries.values()].find((p) => p.preset && p.ace) ?? [...summaries.values()].find((p) => p.ace) ?? [...summaries.values()][0];
  if (result) { showView("concert"); $("#result").scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth" }); void play(); }
  else if (first) await openPiece(first.id);
}
$<HTMLButtonElement>("#programme-listen").addEventListener("click", () => void hearPiece());

const map = new AtlasMap($<SVGSVGElement>("#map"), countries, catalog.points, sites, clickSite);
let composer: Composer | undefined;
composer = new Composer(catalog, () => orchestraChanged());

/* ---------- legend and zoom presets ---------- */
// Species in the table's order (baleen, toothed whales, dolphins), only those with a recording.
const present = new Set(catalog.sources.map((s) => s.species));
const speciesLegend = (Object.keys(SPECIES) as Species[]).filter((species) => present.has(species))
  .map((species) => {
    const instrument = catalog.species[species]?.instrument ?? "";
    return `<span title="${esc(capital(species))}: guide played as ${esc(instrument)}"><i class="swatch" style="--c:${SPECIES_COLOR[species]}"></i>${species}${instrument ? ` ${instrumentIcon(instrument)}` : ""}</span>`;
  }).join("");
$<HTMLElement>("#legend").innerHTML =
  `<div class="legend-row" role="group" aria-label="Species"><span class="legend-label">Species</span>${speciesLegend}</div>` +
  `<div class="legend-row" role="group" aria-label="Conservation status" title="IUCN Red List category of the species, or the listing of the recorded population where it has its own"><span class="legend-label">Conservation status</span>${(["CR", "EN", "VU"] as const).map((c) => `<span><i class="status-chip ${c.toLowerCase()}">${c}</i>${esc(CATEGORY[c])}</span>`).join("")}</div>`;

const near = (lon0: number, lon1: number, lat0: number, lat1: number) =>
  sites.filter((s) => s.location.lon >= lon0 && s.location.lon <= lon1 && s.location.lat >= lat0 && s.location.lat <= lat1);
const zooms: { label: string; sites: Site[] | null }[] = [
  { label: "All sites", sites },
  { label: "North-east Pacific", sites: near(-170, -110, 40, 70) },
  { label: "Salish Sea", sites: near(-123.8, -122, 47.6, 49.3) },
  { label: "World", sites: null },
].filter((z) => !z.sites || z.sites.length >= 2);
$<HTMLElement>("#zooms").innerHTML = zooms.map((z, i) => `<button class="zoom" type="button" data-zoom="${i}" aria-pressed="${i === 0}">${esc(z.label)}</button>`).join("");
$<HTMLElement>("#zooms").addEventListener("click", (event) => {
  const button = (event.target as Element).closest<HTMLButtonElement>("[data-zoom]");
  if (!button) return;
  map.fit(zooms[Number(button.dataset.zoom)]!.sites);
  document.querySelectorAll("#zooms [data-zoom]").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
});
map.fit(sites);

/* ---------- seating ---------- */
function toggle(source: Source) {
  if (isStatic || !composer) return;
  if (!composer.has(source.id) && composer.full) {
    say(`All ${composer.max} seats are taken. Remove a player first.`);
    return;
  }
  composer.toggle(source);
}

function seatAll(site: Site) {
  if (!composer) return;
  for (const source of site.sources) if (!composer.has(source.id) && !composer.add(source)) break;
  if (site.sources.some((s) => !composer!.has(s.id))) say(`All ${composer.max} seats are taken.`);
}

/** One recording at a site: a click seats or removes it. Several: a small menu lists them. */
function clickSite(site: Site) {
  if (!isStatic && site.sources.length === 1) {
    closeSite();
    toggle(site.sources[0]!);
    return;
  }
  openSite(site);
}

function openSite(site: Site) {
  openSiteKey = site.key;
  const popover = $<HTMLElement>("#popover");
  const seatedHere = site.sources.filter((s) => composer?.has(s.id)).length;
  const rows = site.sources.map((source) => {
    const seated = composer?.has(source.id) ?? false;
    return `<div class="row" style="--c:${SPECIES_COLOR[source.species]}"><b>${esc(source.title)}</b>
    <span class="small">${esc(capital(source.species))} · ${source.duration_s.toFixed(0)} s · ${source.onsets_s.length} ${source.material === "clicks" ? "clicks" : "calls"} · ${esc(source.note)}</span>
    ${statusRow(source)}
    <div class="row-actions"><button class="btn" type="button" data-listen="${esc(source.id)}" data-label="Listen">Listen</button>
    ${isStatic ? "" : `<button class="btn ${seated ? "seated" : "add"}" type="button" data-toggle="${esc(source.id)}" aria-pressed="${seated}">${seated ? "✓ In the orchestra · remove" : "+ Seat"}</button>`}</div></div>`;
  }).join("");
  const all = !isStatic && seatedHere < site.sources.length
    ? `<button class="btn add" type="button" data-all>+ Seat all ${site.sources.length}</button>` : "";
  popover.innerHTML = `<button class="close" type="button" aria-label="Close">×</button><h3>${esc(site.location.label)}</h3>
    <span class="small">${site.location.precision === "published" ? "Published hydrophone position." : "Approximate site anchor."} ${esc(site.location.note ?? "")}</span>${all}${rows}`;
  const wasHidden = popover.hidden;
  popover.hidden = false;
  const box = $<HTMLElement>("#map-box").getBoundingClientRect();
  const at = map.anchor(site);
  const width = Math.min(340, box.width - 20);
  popover.style.left = `${Math.max(10, Math.min(box.width - width - 10, at.x + 16))}px`;
  popover.style.top = `${Math.max(10, Math.min(box.height - popover.offsetHeight - 10, at.y - 30))}px`;
  map.markOpen(site);
  if (wasHidden) popover.querySelector<HTMLButtonElement>("[data-toggle], [data-listen]")?.focus();
}

function closeSite() {
  openSiteKey = null;
  $<HTMLElement>("#popover").hidden = true;
  map.markOpen(null);
}

$<HTMLElement>("#popover").addEventListener("click", (event) => {
  const target = (event.target as Element).closest<HTMLButtonElement>("button");
  if (!target) return;
  const site = sites.find((s) => s.key === openSiteKey);
  const source = sourceById.get(target.dataset.toggle ?? target.dataset.listen ?? target.dataset.info ?? "");
  if (target.classList.contains("close")) closeSite();
  else if (source && target.dataset.info) openVoiceCard(source);
  else if (target.dataset.all !== undefined && site) seatAll(site);
  else if (source && target.dataset.toggle) toggle(source);
  else if (source && target.dataset.listen) listen(source, target);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeSite();
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !isStatic) { event.preventDefault(); void compose(); }
});
$<SVGSVGElement>("#map").addEventListener("click", () => closeSite());

function resetPreview() {
  document.querySelectorAll<HTMLButtonElement>("[data-listen], [data-example-listen]").forEach((button) => {
    button.textContent = button.dataset.label ?? "Listen";
    button.classList.remove("playing");
    button.setAttribute("aria-pressed", "false");
  });
}

function listen(source: Source, button: HTMLButtonElement, stem: "animal" | "guide" = "animal") {
  const url = fileUrl(source.files[stem].audio);
  if (!preview.paused && preview.src.endsWith(url)) {
    preview.pause();
    resetPreview();
    return;
  }
  if (player.playing) stop();
  resetPreview();
  preview.src = url;
  preview.onended = resetPreview;
  void preview.play().catch(() => {
    resetPreview();
    playbackError("The audio could not load. Please try again.");
  });
  button.textContent = button.dataset.label === "▶" ? "■" : "Stop";
  button.classList.add("playing");
  button.setAttribute("aria-pressed", "true");
}

function playbackError(message: string) {
  const note = $<HTMLElement>("#load-error");
  note.hidden = false;
  note.textContent = message;
}

function pieceTitle(title: string, sources: string[]): string {
  if (title !== sources.join(" + ")) return title;
  return [...new Set(sources.map((id) => sourceById.get(id)?.species ?? id))].map(capital).join(" & ") + " ensemble";
}

/* ---------- the orchestra dock ---------- */
function orchestraChanged() {
  if (!composer) return;
  const ids = composer.ids();
  map.setSeated(new Set(ids.map((id) => siteOf.get(id)?.key ?? "")));
  map.setCombination(composer.mapParts());
  const open = sites.find((s) => s.key === openSiteKey);
  if (open) openSite(open);
  if (flash && ids.length) flash = "";
  renderDock();
}

function renderDock() {
  if (!composer) return;
  composer.setModel(selectedModel, modelRegistry);
  const ids = composer.ids();
  renderHallSeats(isStatic && result ? result.parts.map((part) => part.source) : ids);
  $<HTMLElement>("#seats").textContent = `${ids.length} seated · up to ${composer.max}`;

  const issues = composer.problems();
  const key = specKey(composer.spec(), catalog);
  const combine = $<HTMLButtonElement>("#combine");
  const take = $<HTMLButtonElement>("#take");
  const ready = !busy && ids.length > 0 && issues.length === 0;
  const matchesModel = (manifest: Manifest) => !composer!.aceOn || modelMatches(manifest, modelRegistry.models.find(m=>m.id===selectedModel)!);
  const fresh = result !== null && key === pieceKey && matchesModel(result);
  const savedModel = renderedModels.get(key);
  const available = rendered.has(key) && (!composer.aceOn || Boolean(savedModel) && modelSatisfies(savedModel!, selectedModel))
    && (!result || key !== pieceKey || matchesModel(result));
  const upgrade = result !== null && key === pieceKey && !fresh && Boolean(result.stems.ace);
  $<HTMLSelectElement>("#compose-model").disabled = busy || !composer.aceOn;
  $<HTMLSelectElement>("#compose-model").value = selectedModel;
  const selectedChoice = modelRegistry.models.find(m => m.id === selectedModel)!;
  $<HTMLElement>("#compose-model-note").textContent = selectedChoice.description;
  $<HTMLFieldSetElement>("#ace-task").hidden = selectedChoice.engine !== "ace-step";
  $<HTMLInputElement>("#strength").closest<HTMLElement>(".field")!.hidden = selectedChoice.engine !== "ace-step";
  if (composer.aceOn) $<HTMLElement>("#model-summary").textContent = `${selectedChoice.label}${composer.spec().ace?.seed === undefined ? "" : ` · seed ${composer.spec().ace?.seed}`}`;
  const canGenerate = !isStatic && !runtimeFailed && !catalogUnavailable && generationRuntime?.python_available !== false
    && (!composer.aceOn || selectedChoice.available !== false && (selectedChoice.engine !== "ace-step" || generationRuntime?.ace_available !== false));
  $<HTMLElement>("#compose-progress").hidden = !busy;
  $<HTMLElement>("#compose-error").hidden = !compositionError;
  $<HTMLElement>("#compose-error").textContent = compositionError;
  $<HTMLElement>("#compose-runtime").hidden = !canGenerate || (selectedChoice.device ? selectedChoice.device !== "cpu" : generationRuntime?.cuda_available !== false) || !composer.aceOn || ((fresh || available) && !busy) || ids.length === 0;
  $<HTMLElement>("#compose-runtime").textContent = `${selectedChoice.label} runs on this server's CPU and may take a while. Finished versions are ready to play immediately.`;
  $("#dock").setAttribute("aria-busy", String(busy));
  combine.disabled = !ready || fresh || (!available && !canGenerate);
  combine.textContent = busy ? compositionJob?.state === "queued" ? "Queued…" : "Composing…"
    : compositionError ? "Try again" : available ? "Play saved piece" : upgrade ? `Compose with ${selectedChoice.label}` : result ? "Update piece" : "Compose piece";
  combine.hidden = fresh && !busy;
  combine.classList.toggle("pending", ready && !fresh && result !== null);
  const previous = $<HTMLButtonElement>("#previous-piece");
  const previousId = previousVersions.get(key);
  previous.hidden = !previousId || previousId === result?.id;
  previous.disabled = busy;
  take.disabled = !ready || !composer.aceOn || !canGenerate;
  take.hidden = ids.length === 0;
  $<HTMLButtonElement>("#clear").disabled = busy || ids.length === 0;
  const status = $<HTMLElement>("#status");
  if (busy) {
    status.hidden = false;
    status.classList.remove("bad");
    status.textContent = compositionJob?.state === "queued" ? "Your selected recordings are queued for composition."
      : "Composing from your selected recordings. You can listen below while you wait.";
    renderCompositionProgress();
    return;
  }
  status.classList.toggle("bad", issues.length > 0 && ids.length > 0);
  status.hidden = fresh && !flash;
  status.textContent = flash || (ids.length === 0 ? "Choose a site on the map to fill a chair."
    : issues.length ? issues[0]!
    : fresh ? `“${pieceTitle(result!.title, result!.parts.map((p) => p.source))}” is ready to play.`
    : available ? "This piece is ready to play."
    : selectedChoice.available === false ? `${selectedChoice.label} is unavailable on this server. Choose another model.`
    : upgrade ? `Generate another version with ${selectedChoice.label}; your existing audio is kept.`
    : result ? `Changed since “${pieceTitle(result.title, result.parts.map((p) => p.source))}”.`
    : `${ids.length} player${ids.length === 1 ? "" : "s"} ready.`);
}

function renderHallSeats(ids: string[]) {
  const chairs = Math.min(catalog.combination.max_parts, Math.max(4, ids.length + 1));
  $<HTMLElement>("#hall-seats").style.setProperty("--chair-count", String(chairs));
  $("#hall-seats").setAttribute("aria-label", `${chairs} orchestra chairs`);
  $("#hall-seats").innerHTML = Array.from({ length: chairs }, (_, i) => {
    const source = sourceById.get(ids[i] ?? "");
    const status = source && statusOf(source);
    const animal = source ? `<img class="species-icon" src="${esc(fileUrl(`species/${SPECIES[source.species].icon}.png`))}" alt="" aria-hidden="true"><span class="instrument-badge">${instrumentIcon(source.instrument)}</span>` : "";
    // A threatened voice keeps its status on the chair: a border in the status colour and the category under it.
    const marked = status && THREATENED.has(status.category) ? status : undefined;
    const statusCaption = marked ? `<span class="seat-status">${statusChip(marked)}${TREND_ARROW[marked.trend] ? ` <span title="${esc(marked.trend)}">${TREND_ARROW[marked.trend]}</span>` : ""}</span>` : "";
    const chair = `<span class="seat-figure"><svg viewBox="0 0 36 42" aria-hidden="true"><path d="M8 19V7a4 4 0 0 1 4-4h12a4 4 0 0 1 4 4v12M6 17v13h24V17M9 30v8M27 30v8"/><rect x="9" y="20" width="18" height="8" rx="2"/></svg>${animal}</span><span class="seat-caption">${i + 1}${source ? ` · ${SPECIES[source.species].seat}` : " · Empty"}</span>${statusCaption}`;
    return source ? `<li class="occupied${marked ? ` threatened ${marked.category.toLowerCase()}` : ""}" style="--c:${SPECIES_COLOR[source.species]}"><button type="button" data-chair="${esc(source.id)}" aria-label="About ${esc(source.title)}, guide played as ${esc(source.instrument)}${status ? `. ${esc(statusLine(status))}` : ""}" title="${esc(capital(source.species))} · ${esc(source.location.label)} · guide: ${esc(source.instrument)}${status ? `\n${esc(statusLine(status))}` : ""}">${chair}<span class="seat-location">${esc(source.location.label)}</span></button>${isStatic ? "" : `<button class="seat-remove" type="button" data-remove="${esc(source.id)}" aria-label="Remove ${esc(source.title)} from chair ${i + 1}">×</button>`}</li>` : `<li class="vacant" aria-label="Seat ${i + 1} available">${chair}</li>`;
  }).join("");
}

$("#hall-seats").addEventListener("click", (event) => {
  const remove = (event.target as Element).closest<HTMLButtonElement>("[data-remove]");
  if (remove) { composer?.remove(remove.dataset.remove!); return; }
  const button = (event.target as Element).closest<HTMLButtonElement>("[data-chair]");
  const source = button && sourceById.get(button.dataset.chair ?? "");
  if (button && source) openVoiceCard(source);
});
$("#hall-seats").addEventListener("pointerover", (event) => {
  const id = (event.target as Element).closest<HTMLElement>("[data-chair]")?.dataset.chair;
  map.highlight(id ? siteOf.get(id)?.key ?? null : null);
});
$("#hall-seats").addEventListener("pointerleave", () => map.highlight(null));

/* ---------- voice card: who is singing, and how their population is doing ---------- */
const voiceCard = $<HTMLDialogElement>("#voice-card");

function statusBlock(status: Status, heading: string): string {
  const trend = `${TREND_ARROW[status.trend] ? `${TREND_ARROW[status.trend]} ` : ""}${status.trend === "unknown" ? "Unknown" : capital(TREND_WORD[status.trend])}${status.trendNote ? `: ${status.trendNote}` : ""}`;
  const row = (term: string, value?: string) => value ? `<dt>${term}</dt><dd>${esc(value)}</dd>` : "";
  return `<section class="voice-status"><span class="section-number">${heading}</span>
    <p class="voice-group">${statusChip(status)} <b>${esc(status.group)}</b></p>
    <dl>${row("Status", status.listing)}${row("History", status.history)}${row("Number", status.count)}<dt>Trend</dt><dd>${esc(trend)}</dd>${row("Threats", status.threats)}${row("Protection", status.protection)}</dl></section>`;
}

function openVoiceCard(source: Source) {
  const { population, species, note, noteRefs } = voiceInfo(source);
  const refs = [...(population?.refs ?? []), ...noteRefs, ...species.refs].filter((ref, i, all) => all.findIndex((r) => r.url === ref.url) === i);
  const seated = !isStatic && (composer?.has(source.id) ?? false);
  voiceCard.innerHTML = `<button class="close" type="button" data-close aria-label="Close">×</button>
    <header class="voice-head" style="--c:${SPECIES_COLOR[source.species]}"><img src="${esc(fileUrl(`species/${SPECIES[source.species].icon}.png`))}" alt="" aria-hidden="true">
      <div><span class="section-number">${esc(capital(source.species))} · ${esc(source.location.label)}</span><h3 id="voice-card-title">${esc(source.title)}</h3></div></header>
    ${population ? statusBlock(population, "This population") : ""}
    ${note ? `<p class="voice-note">${esc(note)}</p>` : ""}
    ${statusBlock(species, population || note ? "The species worldwide" : "The species")}
    <section class="voice-refs"><span class="section-number">Sources</span><ul>${refs.map((r) => `<li><a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.label)}</a></li>`).join("")}</ul></section>
    <p class="small">Counts name the year they describe. A population's own listing replaces its species' category on the map and the chair.</p>
    <div class="row-actions"><button class="btn" type="button" data-listen="${esc(source.id)}" data-label="Listen to the recording">Listen to the recording</button>${seated ? `<button class="btn" type="button" data-remove="${esc(source.id)}">Remove from the orchestra</button>` : ""}</div>`;
  voiceCard.showModal();
}

voiceCard.addEventListener("click", (event) => {
  if (event.target === voiceCard) return voiceCard.close(); // a click on the backdrop
  const target = (event.target as Element).closest<HTMLButtonElement>("button");
  const source = target && sourceById.get(target.dataset.listen ?? target.dataset.remove ?? "");
  if (target?.dataset.close !== undefined) voiceCard.close();
  else if (source && target?.dataset.listen) listen(source, target);
  else if (source && target?.dataset.remove) { composer?.remove(source.id); voiceCard.close(); }
});

function say(text: string) {
  flash = text;
  clearTimeout(flashTimer);
  flashTimer = window.setTimeout(() => { flash = ""; renderDock(); }, 3500);
  renderDock();
}

$<HTMLButtonElement>("#clear").addEventListener("click", () => {
  stop();
  preview.pause();
  resetPreview();
  player.rewind();
  result = null;
  pieceKey = null;
  partOn = [];
  flash = "";
  $("#result").setAttribute("hidden", "");
  $("#technical-result").setAttribute("hidden", "");
  $("#programme-status").textContent = "Chairs cleared. Choose recordings on the map or a finished piece.";
  document.querySelectorAll<HTMLButtonElement>("#saved [data-combo]").forEach(button => button.setAttribute("aria-pressed", "false"));
  const url = new URL(location.href);
  url.hash = "";
  history.replaceState(null, "", url);
  composer?.clear();
});
$<HTMLButtonElement>("#combine").addEventListener("click", () => void compose());
$<HTMLButtonElement>("#previous-piece").addEventListener("click", () => {
  const id = composer && previousVersions.get(specKey(composer.spec(), catalog));
  if (id) void openPiece(id);
});
$<HTMLButtonElement>("#take").addEventListener("click", () => void compose(true));
$<HTMLSelectElement>("#compose-model").addEventListener("change", async () => {
  if (busy) return;
  selectedModel = $<HTMLSelectElement>("#compose-model").value;
  if (selectedModel !== "base" && composer?.spec().ace?.task === "lego") {
    $<HTMLInputElement>('input[name="ace-task"][value="cover"]').checked = true;
    $<HTMLFieldSetElement>("#ace-task").dispatchEvent(new Event("change", {bubbles:true}));
  }
  const revision = ++modelChoiceRevision;
  try { sessionStorage.setItem("whales-compose-model", selectedModel); } catch { /* Selection still works. */ }
  compositionError = "";
  renderDock();
  if (result && composer) {
    const key = specKey(composer.spec(), catalog);
    const id = rendered.get(key);
    const manifest = id ? await piece(id, selectedModel).catch(() => null) : null;
    if (manifest && revision === modelChoiceRevision && !busy && key === specKey(composer.spec(), catalog)) {
      renderedModels.set(key, renderedModel(manifest));
      pieceKey = key;
      showResult(manifest, { seat: false, autoplay: false, scroll: false });
    }
  }
});

/* ---------- compose ---------- */
async function compose(newTake = false) {
  if (busy || !composer || composer.count === 0 || composer.problems().length) return;
  if (newTake) composer.newSeed();
  const spec = composer.spec();
  const key = specKey(spec, catalog);
  if (!newTake && result && key === pieceKey && (!spec.ace || modelMatches(result, modelRegistry.models.find(m=>m.id===selectedModel)!))) return;
  const known = newTake ? undefined : rendered.get(key);
  if (known) {
    const manifest = await piece(known, spec.ace ? selectedModel : undefined).catch(() => null);
    if (manifest && (!spec.ace || manifest.stems.ace && modelSatisfies(renderedModel(manifest), selectedModel))) {
      renderedModels.set(key, manifest.stems.ace ? renderedModel(manifest) : null);
      pieceKey = key;
      showResult(manifest, { seat: false, autoplay: true });
      return;
    }
  }
  if (isStatic || runtimeFailed || catalogUnavailable || generationRuntime?.python_available === false
      || (composer.aceOn && modelRegistry.models.find(m => m.id === selectedModel)?.available === false)) {
    showEngineNotice();
    return;
  }
  const log = $<HTMLPreElement>("#log");
  busy = true;
  compositionJob = null;
  compositionError = "";
  compositionProgress = { type: "progress", percent: null, stage: "Starting the composition engine…" };
  showCompositionInputs(spec);
  renderDock();
  log.textContent = "";
  $<HTMLElement>("#compose-progress").scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "center" });
  try {
    const response = await fetch(`/api/combine?model=${spec.ace ? selectedModel : "0"}`, { method: "POST", body: JSON.stringify(spec) });
    await followComposition(response);
  } catch (error) {
    compositionError = `Composition failed: ${(error as Error).message}`;
  } finally {
    busy = false;
    renderDock();
  }
}

function showCompositionInputs(spec: Spec) {
  $<HTMLElement>("#compose-inputs").hidden = false;
  const seats = composer?.ids() ?? [];
  const parts = spec.arrangement === "layer" ? [...spec.parts].sort((a, b) => seats.indexOf(a.source) - seats.indexOf(b.source)) : spec.parts;
  renderSelectedTracks($<HTMLElement>("#compose-input-tracks"), { ...spec, parts }, sourceById, (source, button) => listen(source, button));
}

function renderCompositionProgress() {
  const bar = $<HTMLProgressElement>("#compose-bar");
  const percent = compositionProgress?.percent;
  if (percent == null) bar.removeAttribute("value");
  else bar.value = percent;
  $<HTMLElement>("#compose-stage").textContent = compositionProgress?.stage ?? "Starting the composition engine…";
  $<HTMLElement>("#compose-percent").textContent = percent == null ? "" : `~${Math.floor(percent)}%`;
  const elapsed = compositionJob ? Math.max(0, Math.floor((Date.now() - Date.parse(compositionJob.started_at_utc)) / 1000)) : 0;
  const time = elapsed >= 60 ? `${Math.floor(elapsed / 60)}m ${elapsed % 60}s` : `${elapsed}s`;
  $<HTMLElement>("#compose-elapsed").textContent = `${elapsed ? `${time} elapsed · ` : ""}You can listen to the selected recordings while you wait.`;
}

/** Retry lost streams using the existing job; only a terminal event changes the result. */
async function followComposition(initial: Response, restore = false) {
  let response = initial;
  let restored = !restore;
  const log = $<HTMLPreElement>("#log");
  for (let attempt = 0; ; attempt++) {
    try {
      if (!response.ok) {
        const message = (await response.json().catch(() => ({}))).error ?? `Server answered ${response.status}`;
        if (response.status === 404) {
          rememberJob(null);
          compositionJob = null;
        }
        throw new Error(message);
      }
      await readComposition(response, (message) => {
        if (message.type === "job") {
          compositionJob = message.job;
          selectedModel = message.job.model ?? modelRegistry.default_model;
          rememberJob(message.job.id);
          if (!restored) {
            restored = true;
            composer?.loadSpec(message.job.spec);
            showCompositionInputs(message.job.spec);
          }
          renderDock();
        } else if (message.type === "progress") {
          // Replayed progress also restores the current stage after a reconnect.
          compositionProgress = message;
          renderCompositionProgress();
        } else if (message.type === "log") {
          log.textContent += message.text + "\n";
          log.scrollTop = log.scrollHeight;
        } else if (message.type === "error") {
          rememberJob(null);
          compositionError = `Composition failed: ${message.message}`;
          log.textContent += compositionError + "\n";
        } else if (message.type === "done") {
          rememberJob(null);
          pieceKey = specKey(compositionJob?.spec ?? message.manifest.spec, catalog);
          rendered.set(pieceKey, message.manifest.id);
          renderedModels.set(pieceKey, message.manifest.stems.ace ? renderedModel(message.manifest) : null);
          compositionError = "";
          busy = false;
          $<HTMLElement>("#compose-inputs").hidden = true;
          showResult(message.manifest, { seat: false, autoplay: true });
          void loadSaved();
        }
      });
      return;
    } catch (error) {
      if (!compositionJob || attempt >= 2) throw new Error(compositionJob
        ? "The progress connection was lost. Your piece may still be composing. Try again to reconnect."
        : (error as Error).message);
      compositionProgress = { type: "progress", percent: compositionProgress?.percent ?? null, stage: "Reconnecting to your composition…" };
      renderCompositionProgress();
      await new Promise((resolve) => setTimeout(resolve, 1000));
      log.textContent = ""; // The job stream replays recent diagnostics.
      response = await fetch(`/api/combine/${compositionJob.id}`).catch(() => new Response(null, { status: 503 }));
    }
  }
}

async function resumeComposition() {
  let id: string | null = null;
  try { id = sessionStorage.getItem(JOB_STORAGE); } catch { return; }
  if (!id || busy) return;
  busy = true;
  compositionError = "";
  compositionProgress = { type: "progress", percent: null, stage: "Reconnecting to your composition…" };
  $<HTMLPreElement>("#log").textContent = "";
  renderDock();
  try { await followComposition(await fetch(`/api/combine/${encodeURIComponent(id)}`), true); }
  catch (error) { compositionError = (error as Error).message; }
  finally { busy = false; renderDock(); }
}

if (!isStatic) setInterval(() => { if (busy) renderCompositionProgress(); }, 1000);

/* ---------- result and playback ---------- */
function showResult(manifest: Manifest | null, options: { seat: boolean; autoplay?: boolean; scroll?: boolean }) {
  if (!manifest) return;
  compositionError = "";
  stop();
  player.rewind();
  result = manifest;
  if (options.scroll !== false) showView("concert");
  $("#load-error").setAttribute("hidden", "");
  $<HTMLButtonElement>("#programme-listen").disabled = false;
  if (options.seat && composer && !isStatic) {
    composer.load(manifest);
    pieceKey = specKey(manifest.spec, catalog);
    rendered.set(pieceKey, manifest.id);
    renderedModels.set(pieceKey, manifest.stems.ace ? renderedModel(manifest) : null);
    const updatedKey = specKey(composer.spec(), catalog);
    if (updatedKey !== pieceKey) previousVersions.set(updatedKey, manifest.id);
  }
  partOn = manifest.parts.map(() => true);
  stemOn.ace = Boolean(manifest.stems.ace);
  stemOn.response = !manifest.stems.ace;
  if (isStatic || !composer) map.setCombination(resultParts());
  const section = $<HTMLElement>("#result");
  section.hidden = false;
  $<HTMLElement>("#result-eyebrow").textContent = `Current piece / ${manifest.spec.arrangement === "layer" ? "Together" : "In sequence"} · ${manifest.parts.length} player${manifest.parts.length === 1 ? "" : "s"} · ${manifest.duration_s.toFixed(1)} s${manifest.stems.ace ? ` · ${modelLabel(renderedModel(manifest), modelRegistry)} · seed ${manifest.spec.ace?.seed ?? ""}` : ""}`;
  $<HTMLElement>("#result-title").textContent = pieceTitle(manifest.title, manifest.parts.map((p) => p.source));
  const resultNote = document.querySelector<HTMLElement>("#result-note");
  if (resultNote) resultNote.textContent = manifest.interpretation;
  $<HTMLInputElement>("#preset-name").value = manifest.preset ? manifest.title : "";
  $<HTMLElement>("#preset-status").textContent = manifest.preset ? "Saved as a preset." : "";
  const stems = (Object.keys(STEM_LABELS) as StemName[]).filter((key) => manifest.stems[key]);
  $<HTMLElement>("#stems").innerHTML = "<legend>Listen to</legend>" + stems.map((key) =>
    `<button class="toggle" type="button" data-stem="${key}" aria-pressed="${stemOn[key]}">${STEM_LABELS[key]}</button>`).join("");
  $<HTMLElement>("#part-toggles").innerHTML = "<legend>Players</legend>" + manifest.parts.map((p, i) =>
    `<button class="toggle" type="button" data-part="${i}" aria-pressed="true" title="${esc(p.title)} · guide: ${esc(p.instrument)} · Mute this player's recording and guide"><i class="swatch" style="--c:${partColor(p)}"></i>${i + 1} ${esc(capital(p.species))} ${instrumentIcon(p.instrument)}</button>`).join("");
  $<HTMLElement>("#individual-track-count").textContent = `${manifest.parts.length} recordings · shared timeline`;
  $<HTMLDetailsElement>("details.individual-tracks").open = true; // each new piece shows its voices, even if the last one was folded
  setIndividualPlayhead = renderIndividualTracks($<HTMLElement>("#individual-lanes"), manifest, sourceById, (t) => void play(t));
  setPlayhead = renderLanes($<HTMLElement>("#lanes"), manifest, (t) => void play(t), ["guide", manifest.stems.ace ? "ace" : "response"]);
  document.querySelectorAll<HTMLButtonElement>("#saved [data-combo]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.combo === manifest.id)));
  renderScores($<HTMLElement>("#scores"), manifest);
  renderTechnicalOutput(manifest);
  $<HTMLElement>("#clock").textContent = `0.0 / ${manifest.duration_s.toFixed(1)} s`;
  $<HTMLElement>("#technical-clock").textContent = `0.0 / ${manifest.duration_s.toFixed(1)} s`;
  const hash = manifest.id + (manifest.stems.ace && !isStatic ? `~${renderedModel(manifest)}` : "");
  if (location.hash.slice(1) !== hash) history.replaceState(null, "", `#${hash}`);
  if (options.scroll !== false) section.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
  renderDock();
  if (options.autoplay) void play(0);
}

function renderTechnicalOutput(manifest: Manifest) {
  const section = $<HTMLElement>("#technical-result");
  section.hidden = false;
  $<HTMLElement>("#technical-result-title").textContent = pieceTitle(manifest.title, manifest.parts.map((p) => p.source));
  const note = document.querySelector<HTMLElement>("#result-note");
  if (note) note.textContent = `${manifest.interpretation} Coloured ticks reference measured source onsets. Pitch contours appear on the guide; the generated spectrogram shows the model's output.`;
  setTechnicalPlayhead = renderLanes($<HTMLElement>("#technical-lanes"), manifest, (t) => void play(t), ["guide", "response", "ace"]);
}

function resultParts(): MapPart[] {
  return result ? result.parts.map((p) => ({ location: p.location, species: p.species })) : [];
}

function gains(): Record<string, number> {
  const out: Record<string, number> = {};
  if (!result) return out;
  result.parts.forEach((_, i) => {
    out[`animal-${i}`] = stemOn.animal && partOn[i] ? 1 : 0;
    out[`guide-${i}`] = stemOn.guide && partOn[i] ? 1 : 0;
  });
  out.response = stemOn.response ? 1 : 0;
  out.ace = stemOn.ace ? 1 : 0;
  return out;
}

function tracks(): Track[] {
  if (!result) return [];
  const g = gains();
  const out: Track[] = result.parts.flatMap((p, i) => [
    { key: `animal-${i}`, url: fileUrl(p.files.animal), gain: g[`animal-${i}`] ?? 0 },
    { key: `guide-${i}`, url: fileUrl(p.files.guide), gain: g[`guide-${i}`] ?? 0 },
  ]);
  for (const key of ["response", "ace"] as const) {
    const stem = result.stems[key];
    if (stem) out.push({ key, url: fileUrl(stem.audio), gain: g[key] ?? 0 });
  }
  return out;
}

/** Concert and technical views share the same playback state. */
function playButtons(state: "play" | "pause" | "loading") {
  for (const button of document.querySelectorAll<HTMLButtonElement>("#play, #technical-play")) {
    button.setAttribute("aria-pressed", String(state === "pause"));
    button.textContent = state === "loading" ? "Loading…" : state === "pause" ? "❚❚ Pause" : "▶ Play";
  }
}

async function play(offset?: number) {
  if (!result) return;
  const manifest = result;
  preview.pause();
  resetPreview();
  playButtons("loading");
  try {
    await player.play(tracks(), offset ?? player.time, manifest.duration_s);
    if (result !== manifest || !player.playing) return;
    playButtons("pause");
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(tick);
  } catch (error) {
    if (result !== manifest) return;
    playButtons("play");
    playbackError(`Audio could not load: ${(error as Error).message}`);
  }
}

function stop() {
  player.stop();
  cancelAnimationFrame(frame);
  playButtons("play");
  map.clearPlayback();
}

player.onEnded = () => {
  stop();
  setPlayhead(null);
  setIndividualPlayhead(null);
  setTechnicalPlayhead(null);
};

function tick() {
  if (!result || !player.playing) return;
  const t = player.time;
  const onsets = result.parts.map((p) => p.onsets_s);
  const spans = result.parts.map((p) => ({ start_s: p.offset_s, length_s: p.trim_s[1] - p.trim_s[0] }));
  map.setPlayback(reducedMotion() ? [] : activePulses(onsets, t, PULSE_LIFETIME, (i) => partOn[i] ?? false), PULSE_LIFETIME,
    soundingParts(spans, t).filter((i) => partOn[i]), resultParts());
  setPlayhead(t);
  setIndividualPlayhead(t);
  setTechnicalPlayhead(t);
  $<HTMLElement>("#clock").textContent = `${t.toFixed(1)} / ${result.duration_s.toFixed(1)} s`;
  $<HTMLElement>("#technical-clock").textContent = `${t.toFixed(1)} / ${result.duration_s.toFixed(1)} s`;
  frame = requestAnimationFrame(tick);
}

for (const id of ["#play", "#technical-play"]) $<HTMLButtonElement>(id).addEventListener("click", () => (player.playing ? stop() : void play()));
$<HTMLElement>("#stems").addEventListener("click", (event) => {
  const button = (event.target as Element).closest<HTMLButtonElement>("[data-stem]");
  if (!button) return;
  const key = button.dataset.stem as StemName;
  stemOn[key] = !stemOn[key];
  button.setAttribute("aria-pressed", String(stemOn[key]));
  for (const [track, gain] of Object.entries(gains())) player.setGain(track, gain);
});
$<HTMLElement>("#part-toggles").addEventListener("click", (event) => {
  const button = (event.target as Element).closest<HTMLButtonElement>("[data-part]");
  if (!button) return;
  const i = Number(button.dataset.part);
  partOn[i] = !partOn[i];
  button.setAttribute("aria-pressed", String(partOn[i]));
  document.querySelector<HTMLElement>(`[data-track-part="${i}"]`)?.classList.toggle("muted", !partOn[i]);
  for (const [track, gain] of Object.entries(gains())) player.setGain(track, gain);
});

/* ---------- presets ---------- */
// Naming happens only here, after a piece exists, so it never slows down putting an orchestra together.
$<HTMLFormElement>("#preset-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!result || isStatic) return;
  const name = $<HTMLInputElement>("#preset-name").value.trim();
  const status = $<HTMLElement>("#preset-status");
  try {
    await json(`/api/combos/${result.id}/preset`, { method: "POST", body: JSON.stringify({ name }) });
    result = { ...result, title: name || result.title, preset: Boolean(name) };
    if (name) $<HTMLElement>("#result-title").textContent = name;
    $<HTMLElement>("#technical-result-title").textContent = result.title;
    renderDock();
    status.textContent = name ? "Saved as a preset." : "Removed from presets.";
    void loadSaved();
  } catch (error) {
    status.textContent = `Could not save: ${(error as Error).message}`;
  }
});

/* ---------- presets and recent pieces ---------- */
function savedItem(c: ComboSummary): string {
  const species = [...new Set(c.sources.map((id) => sourceById.get(id)?.species ?? id))].map(capital);
  return `<li><button type="button" data-combo="${c.id}" aria-pressed="${c.id === result?.id}" class="${c.preset ? "is-preset" : ""}" title="Fill the orchestra with this piece's players"><b><span class="go" aria-hidden="true">+</span>${c.preset ? "★ " : ""}${esc(pieceTitle(c.title, c.sources))}</b>
    <span>${species.map(esc).join(" + ")}</span>
    <span class="piece-meta">${c.sources.length} voices · ${c.duration_s.toFixed(0)} seconds · ${c.ace ? "Generated composition" : "Constructed response"}</span></button></li>`;
}

let summaries = new Map<string, ComboSummary>();
let programmeUnavailable = false;

/** A saved piece. The server already applies preset names; the published copy applies them from combos.json. */
async function piece(id: string, model?: string): Promise<Manifest> {
  const manifest = await json<Manifest>(comboUrl(id) + (model && !isStatic ? `?model=${encodeURIComponent(model)}` : ""));
  const summary = summaries.get(id);
  return isStatic && summary?.preset ? { ...manifest, title: summary.title, preset: true } : manifest;
}

async function loadSaved() {
  let saved: ComboSummary[];
  try {
    saved = await json<ComboSummary[]>(combosUrl(), { signal: AbortSignal.timeout(10_000) });
    if (programmeUnavailable) $<HTMLElement>("#programme-status").textContent = "";
    programmeUnavailable = false;
  } catch {
    programmeUnavailable = true;
    $<HTMLElement>("#programme-status").textContent = "The listening programme could not load. Please retry the connection or reload the page.";
    if (!summaries.size) {
      $<HTMLElement>("#saved").textContent = "";
      $<HTMLButtonElement>("#programme-listen").disabled = !result;
    }
    return;
  }
  summaries = new Map(saved.map((c) => [c.id, c]));
  const programme = saved.filter((c) => !precomputedIds.has(c.id) || c.preset || c.id === result?.id);
  $<HTMLButtonElement>("#programme-listen").disabled = programme.length === 0 && !result;
  const presets = programme.filter((c) => c.preset).sort((a, b) => b.created_at_utc.localeCompare(a.created_at_utc));
  const recent = programme.filter((c) => !c.preset);
  const group = (title: string, items: ComboSummary[]) => items.length ? `<li class="group-title">${title}</li>${items.map(savedItem).join("")}` : "";
  $<HTMLElement>("#saved").innerHTML = programme.length
    ? (presets.length ? group("Presets", presets) + group("Recent pieces", recent) : recent.map(savedItem).join(""))
    : `<li class="small">None yet. Seat some players and compose; each piece appears here.</li>`;
}
async function openPiece(id: string, options: { autoplay?: boolean; scroll?: boolean } = {}) {
  if (busy) { say("Wait for the current composition to finish before loading another ensemble."); return false; }
  try { showResult(await piece(id), { seat: true, autoplay: options.autoplay ?? true, scroll: options.scroll }); return true; }
  catch (error) { playbackError(`This piece could not open: ${(error as Error).message}`); return false; }
}
$<HTMLElement>("#saved").addEventListener("click", async (event) => {
  const button = (event.target as Element).closest<HTMLButtonElement>("[data-combo]");
  if (button && await openPiece(button.dataset.combo!, { autoplay: false, scroll: false })) {
    $<HTMLButtonElement>('#zooms [data-zoom="0"]').click();
    $("#programme-status").textContent = `${result!.parts.length} players seated. Play below, or change the ensemble.`;
    if (matchMedia("(max-width: 850px)").matches) {
      setProgrammeOpen(false);
      $<HTMLButtonElement>("#play").focus({ preventScroll: true });
      $("#hall-seats").scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
    }
  }
});

// The published copy uses the same programme drawer; composition controls stay hidden.
if (isStatic) {
  document.body.classList.add("static");
  $<HTMLElement>("#map-hint").textContent = "Scroll or pinch to zoom. Select a site to hear its recordings.";
}
dockReady = true;
orchestraChanged();
void refreshCatalog();
void loadPrecomputed();
void refreshModels();
setInterval(() => { if (document.visibilityState === "visible") { void refreshCatalog(); void refreshModels(); } }, 5 * 60_000);
await loadSaved();
if (isStatic && !location.hash) {
  const first = document.querySelector<HTMLButtonElement>("#saved [data-combo]");
  if (first) showResult(await piece(first.dataset.combo!), { seat: false, scroll: false }); // land at the top of the page
}

// A bare #<piece id> opens that piece and seats its players, so pieces can be linked.
async function openFromHash(scroll = true) {
  const [id, model] = location.hash.slice(1).split("~");
  if (id && /^[0-9a-f]{12}$/.test(id) && (!model || /^[a-z][a-z0-9-]{0,63}$/.test(model))
      && (id !== result?.id || result && model && model !== renderedModel(result))) {
    const manifest = await piece(id, model).catch(() => null);
    if (manifest) { selectedModel = model ?? renderedModel(manifest); showResult(manifest, { seat: true, scroll }); }
  }
}
window.addEventListener("hashchange", () => void openFromHash());
await openFromHash(false);
try {
  const draft = sessionStorage.getItem("whales-catalog-draft");
  sessionStorage.removeItem("whales-catalog-draft");
  if (draft) composer?.loadSpec(JSON.parse(draft) as Spec);
} catch { /* A failed browser cache must not stop playback. */ }
if (!isStatic) void resumeComposition();
