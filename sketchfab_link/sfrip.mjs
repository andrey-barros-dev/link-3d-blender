#!/usr/bin/env node
/**
 * sfrip — rip a Sketchfab model into a clean, self-contained GLB
 * (geometry + PBR textures + animations + rig), without a browser extension.
 *
 * Reuses the pure core from sfdl (sketchfab-public-downloader):
 *   decrypt.js  sf-api.js  osgjs2gltf.js  descramble.js  orm.js
 *   osg-codec.js  osg-scene.js  skin.js
 * The extension-specific bits (chrome.storage, OffscreenCanvas) are stubbed /
 * polyfilled so the same code runs under Node.
 *
 * Usage:
 *   node sfrip.mjs <sketchfab-model-url-or-uid> [options]
 *   node sfrip.mjs <url> --out ./out --no-textures --max-edge 2048 --zip
 *
 * Output (one folder per model, e.g. out/<name>-<uid8>/):
 *   <name>.glb          self-contained GLB (textures + animations embedded)
 *   textures/*.png      loose PBR maps (baseColor / normal / ORM / …)
 *   model.gltf+bin      external glTF (when textures are external)
 *   info.json           source metadata + rip summary
 *   README.txt
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** The package's own version, so the two cannot drift apart. */
const VERSION = (() => {
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "package.json"), "utf8"));
    return pkg.version || "0.0.0";
  } catch (_) {
    return "0.0.0";
  }
})();

// ---------------------------------------------------------------------------
// Browser API polyfills (PNG + JPEG) — needed by orm.js packOrmTexture/resample
// and osgjs2gltf.js (flipNormalMapY / bakeOpacityIntoAlbedo). Node has no
// OffscreenCanvas / createImageBitmap, so we back them with the pure PNG
// codec in the lib plus jpeg-js (pure JS) for JPEG maps.
// ---------------------------------------------------------------------------
import { decodePngRgba, encodePngRgba } from "./lib/descramble.js";
import * as jpeg from "jpeg-js";

const isPngBytes = (b) =>
  b && b.length >= 8 && b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47;
const isJpegBytes = (b) =>
  b && b.length >= 3 && b[0] === 0xff && b[1] === 0xd8;

class ImageBitmapPoly {
  constructor(rgba, w, h) { this.width = w; this.height = h; this._rgba = rgba; }
  close() {}
}
class ImageDataPoly {
  constructor(data, w, h) { this.data = data; this.width = w; this.height = h; }
}
class OffscreenCanvasPoly {
  constructor(w, h) {
    this.width = w; this.height = h;
    this._px = new Uint8ClampedArray(w * h * 4);
  }
  getContext() {
    const self = this;
    return {
      drawImage(bmp, dx, dy, dw, dh) {
        dw = dw || self.width; dh = dh || self.height;
        const src = bmp._rgba, sw = bmp.width, sh = bmp.height;
        for (let y = 0; y < dh; y++) {
          const sy = Math.min(sh - 1, Math.floor((y * sh) / dh));
          for (let x = 0; x < dw; x++) {
            const sx = Math.min(sw - 1, Math.floor((x * sw) / dw));
            const si = (sy * sw + sx) * 4, di = (y * dw + x) * 4;
            self._px[di] = src[si]; self._px[di + 1] = src[si + 1];
            self._px[di + 2] = src[si + 2]; self._px[di + 3] = src[si + 3];
          }
        }
      },
      getImageData(x, y, w, h) {
        const start = (y * w + x) * 4;
        return new ImageDataPoly(self._px.subarray(start, start + w * h * 4), w, h);
      },
      putImageData(img, x, y) { self._px.set(img.data, (y * img.width + x) * 4); },
    };
  }
  async convertToBlob() {
    const png = await encodePngRgba(this._px, this.width, this.height);
    return new Blob([png], { type: "image/png" });
  }
}

if (typeof globalThis.createImageBitmap !== "function") {
  globalThis.createImageBitmap = async (blob) => {
    const buf = new Uint8Array(await blob.arrayBuffer());
    if (isJpegBytes(buf)) {
      const { data, width, height } = jpeg.decode(buf, { useTArray: true });
      return new ImageBitmapPoly(new Uint8ClampedArray(data.buffer, data.byteOffset, data.byteLength), width, height);
    }
    if (!isPngBytes(buf)) throw new Error("polyfill createImageBitmap: only PNG/JPEG is supported");
    const { rgba, width, height } = await decodePngRgba(buf);
    return new ImageBitmapPoly(rgba, width, height);
  };
}
if (typeof globalThis.OffscreenCanvas !== "function") globalThis.OffscreenCanvas = OffscreenCanvasPoly;
if (typeof globalThis.ImageData !== "function") globalThis.ImageData = ImageDataPoly;

// chrome stub — extractStaticKey / settings.js / devlog.js call chrome.storage
// inside try/catch; give them a no-op in-memory store so nothing throws.
if (typeof globalThis.chrome === "undefined") {
  const store = {};
  globalThis.chrome = {
    storage: {
      local: {
        async get(k) {
          if (typeof k === "string") return { [k]: store[k] };
          const keys = Array.isArray(k) ? k : Object.keys(store);
          const out = {}; for (const key of keys) out[key] = store[key]; return out;
        },
        async set(o) { Object.assign(store, o); },
      },
      session: {
        async set(o) { Object.assign(store, o); },
        async get() { return {}; },
      },
    },
    runtime: { getURL: (p) => p, id: "sfrip" },
  };
}

// ---------------------------------------------------------------------------
// Core (pure) imports
// ---------------------------------------------------------------------------
import {
  extractUid, fetchViewerInfo, getDiterB, extractStaticKey, isBinzFormat,
  modelBinzUrls, fetchAnimations, fetchTextures, fetchBytes,
  parseViewerMaterials, viewerOrientation, viewerLights, sanitizeName, pickBestImage,
} from "./lib/sf-api.js";
import { decryptBinzBytes, outNameFor } from "./lib/decrypt.js";
import {
  convertOsgjsToGlbFromFiles, resolveUidToBase, classifyTextureName, textureBasename,
  previewMergeLevels, MERGE_LEVELS,
} from "./lib/osgjs2gltf.js";
import { planOrm, packOrmTexture } from "./lib/orm.js";
import { buildZip } from "./lib/zip.js";
import { c, bytes, num, duration, Progress, Spinner, pad, heading, row, menu } from "./lib/cli-ui.js";

// ---------------------------------------------------------------------------
// packOrmMaps — copied from pipeline.js (not exported there). Packs each
// material's separate metal/rough maps into one glTF ORM texture.
// ---------------------------------------------------------------------------
async function packOrmMaps(viewerMaterials, textures, report, maxTextureEdge) {
  if (!viewerMaterials || !viewerMaterials.size || !textures.length) return textures;
  const byBase = new Map();
  for (const t of textures) byBase.set(textureBasename(t.name).toLowerCase(), t);
  const resolve = (uid) => resolveUidToBase(uid, textures);
  const entryFor = (base) => (base ? byBase.get(textureBasename(base).toLowerCase()) || null : null);

  const added = [];
  let packed = 0, reused = 0;
  const cache = new Map();

  for (const spec of viewerMaterials.values()) {
    const plan = planOrm(spec, resolve, classifyTextureName);
    if (plan.action !== "pack") continue;
    const key = `${plan.metalName || "-"}|${plan.roughName || "-"}|${plan.invertRough ? 1 : 0}`;
    if (cache.has(key)) { spec.packedOrmName = cache.get(key); reused++; continue; }
    const metal = entryFor(plan.metalName);
    const rough = entryFor(plan.roughName);
    if (!metal && !rough) continue;
    const stem = String(plan.metalName || plan.roughName)
      .replace(/\.[^.]+$/, "").replace(/_(metallic|metalness|metal|roughness|rough|gloss|glossiness)$/i, "");
    const outName = `textures/${stem}_ORM.png`;
    try {
      const result = await packOrmTexture({ metal, rough, invertRough: plan.invertRough, maxEdge: maxTextureEdge });
      added.push({ name: outName, data: result.data, uid: "", originalName: `${stem}_ORM.png`, width: result.width, height: result.height, fromOrmPack: true });
      byBase.set(`${stem}_ORM.png`.toLowerCase(), added[added.length - 1]);
      cache.set(key, `${stem}_ORM.png`);
      spec.packedOrmName = `${stem}_ORM.png`;
      packed++;
    } catch (e) {
      report(`ORM pack failed for ${spec.name || "?"}: ${e && e.message ? e.message : e}`);
    }
  }

  if (packed || reused) report(`Packed ${packed} ORM map(s) from separate metal/rough maps` + (reused ? ` (${reused} shared)` : ""));
  return added.length ? textures.concat(added) : textures;
}

const MERGE_KEYS = new Set(["off", "objects", "groups", "materials", "all"]);

// ---------------------------------------------------------------------------
// The rip
// ---------------------------------------------------------------------------
async function rip(pageUrl, outDir, options = {}) {
  const t0 = Date.now();
  /** Run time minus the seconds spent waiting on the user. */
  let idleMs = 0;
  const logLines = [];
  const quiet = options.quiet === true;
  const verbose = options.verbose === true;

  /**
   * Every line of the run goes into the log; only some go to the terminal.
   *
   * The distinction matters. A user wants to know the model, what is being
   * fetched, and where it landed. They do not want PNG descramble offsets,
   * per-material factor dumps, or the internal fetch order -- those are for
   * `--verbose`, and for `info.json` where they can be read after the fact.
   */
  const log = (line) => { logLines.push(String(line ?? '')); };
  /** A line for the user. */
  const step = (msg) => {
    const line = String(msg ?? '');
    logLines.push(line);
    if (!quiet) console.log(line);
  };
  /** Detail for the log, and for `--verbose`. */
  const detail = (msg) => {
    const line = String(msg ?? '');
    logLines.push(line);
    if (!quiet && verbose) console.log(c.gray(`  ${line}`));
  };

  const wantTextures = options.downloadTextures !== false;
  const maxTextureEdge = options.maxTextureEdge === 2048 || options.maxTextureEdge === 4096 ? options.maxTextureEdge : 0;
  const concurrency = Math.max(1, options.concurrency || 3);

  const uid = extractUid(pageUrl);
  if (!uid) throw new Error("Not a Sketchfab model URL (need a 32-hex model id).");
  log(`UID: ${uid}`);

  const spin = new Spinner("Reading model data", { enabled: !quiet });
  const { info, embedHtml, prefetched } = await fetchViewerInfo(uid);
  spin.stop();
  const name = sanitizeName(info.name || uid);
  const author = (info.user && (info.user.displayName || info.user.username)) || "unknown";
  log(`Model: ${info.name || name} (by ${author})`);

  const viewerMaterials = parseViewerMaterials(info);
  log(`Viewer materials: ${viewerMaterials.size}`);
  const skipCount = [...viewerMaterials.values()].filter((s) => s.skipMesh).length;
  if (skipCount) log(`  (${skipCount} glass/invisible material(s) will be skipped)`);

  if (!isBinzFormat(info)) throw new Error("Not in the current encrypted .binz format (or private/legacy).");

  const keySpin = new Spinner("Preparing decryptor", { enabled: !quiet });
  const staticKey = await extractStaticKey(embedHtml, (m) => log(String(m)));
  keySpin.stop();
  log(`Static key: ${staticKey.slice(0, 8)}… (${staticKey.length} hex)`);

  const wasmPath = path.join(__dirname, "lib", "wasm", "decrypt.wasm");
  const wasmBytes = new Uint8Array(fs.readFileSync(wasmPath));
  log(`WASM loaded (${wasmBytes.length} bytes)`);

  const diterB = getDiterB(info);
  log(`diter.b length: ${String(diterB || "").length}`);

  const urls = modelBinzUrls(info);
  const decrypted = {};
  const dlProgress = new Progress("model files", urls.length, { enabled: !quiet });
  for (const url of urls) {
    const binzName = url.split("/").pop().split("?")[0];
    dlProgress.setLabel(binzName.replace(/\.binz?$/, "").slice(0, 22));
    let enc;
    try {
      enc = await fetchBytes(url);
    } catch (e) {
      if (binzName.includes("wireframe")) {
        dlProgress.tick();
        detail(`skip ${binzName} (not available)`);
        continue;
      }
      dlProgress.finish();
      throw new Error(`Download ${binzName} failed: ${e && e.message ? e.message : e}`);
    }
    detail(`decrypting ${binzName} (${enc.length} bytes)`);
    let plain;
    try {
      plain = await decryptBinzBytes(wasmBytes, enc, diterB, staticKey);
    } catch (e) {
      dlProgress.finish();
      throw new Error(`Decrypt ${binzName} failed: ${e && e.message ? e.message : e}`);
    }
    const outName = outNameFor(binzName);
    decrypted[outName] = plain;
    detail(`${outName} (${plain.length} bytes)`);
    dlProgress.tick();
  }
  dlProgress.finish();

  if (!decrypted["file.osgjs"] || !decrypted["model_file.bin"]) {
    throw new Error(`Missing file.osgjs or model_file.bin after decrypt. Have: ${Object.keys(decrypted).join(", ")}`);
  }

  /**
   * The model, described before anything else is downloaded.
   *
   * Geometry counts and the merge choice are the two things a person needs to
   * decide whether to continue, and both are known the moment the scene is
   * decrypted. Textures are the slow part, so the panel goes here rather than
   * after them.
   */
  let preview = null;
  try {
    const pv = new Spinner("Analysing scene", { enabled: !quiet });
    preview = await previewMergeLevels(
      decrypted["file.osgjs"], decrypted["model_file.bin"],
      decrypted["model_file_wireframe.bin"] || null, [], viewerMaterials
    );
    pv.stop();
  } catch (e) {
    log(`Scene analysis skipped: ${e && e.message ? e.message : e}`);
  }

  /**
   * What this run will produce, shown before it commits to anything.
   *
   * Geometry and the merge choice are known the moment the scene is decrypted,
   * and the output path is known before that. Textures are the slow part, so
   * this is the place to decide whether to continue.
   */
  const outName = `${name}-${uid.slice(0, 8)}`;
  const outPath = path.resolve(outDir, outName);

  if (preview && !quiet) {
    const animCount = (info.options && info.options.animation && info.options.animation.order || []).length;
    console.log(heading(info.name || name));
    console.log(row("by", c.cyan(author)));
    console.log(row("geometry", `${num(preview.primitives)} parts · ${num(preview.vertices)} verts · ${num(preview.triangles)} tris`));
    console.log(row("materials", String(preview.materials)));
    console.log(row("rig", preview.hasRig
      ? `${preview.skins} skeleton(s), ${num(preview.bones)} bones`
      : c.gray("none")));
    console.log(row("animation", animCount ? `${animCount} clip(s)` : c.gray("none")));
    console.log(row("textures", wantTextures
      ? `${viewerMaterials.size} to download` + (skipCount ? `, ${skipCount} invisible skipped` : "")
      : c.gray("skipped (--no-textures)")));
    console.log(row("output", c.cyan(outPath)));
    console.log();

    /**
     * Warn before a big download rather than after it.
     *
     * Measured rather than guessed: the viewer's texture list carries the byte
     * size of every resolution variant it offers, so the best one per map sums
     * to roughly what will be fetched. A first attempt at this used a flat
     * per-map allowance, which is why the number is worth computing properly.
     *
     * Said as "about" because the figure is a floor: the CDN serves compressed
     * originals, while the rip writes unscrambled PNG, so the folder ends up
     * larger than this. On one model the estimate was 40 MB against 62 MB on
     * disk. Understating slightly is the safe direction for a warning.
     */
    const sceneBytes = Object.values(decrypted)
      .reduce((n, b) => n + (b && b.length ? b.length : 0), 0);
    let textureBytes = 0;
    if (wantTextures) {
      const listing = prefetched[`/i/models/${uid}/textures?optimized=1`];
      for (const t of (listing && listing.results) || []) {
        const best = pickBestImage(t.images || [], maxTextureEdge, null);
        if (best && best.size) textureBytes += best.size;
      }
    }
    const estimate = sceneBytes + textureBytes;
    if (estimate > 400 * 1024 * 1024) {
      const note = estimate > 1024 * 1024 * 1024 ? "large download" : "sizable download";
      console.log(`  ${c.yellow("!")} ${c.gray(`${note}: about ${bytes(estimate)}, mostly textures.`)}`);
      console.log(`  ${c.gray("  Use --no-textures for geometry only, or --merge=groups to cut the mesh count.")}`);
      console.log();
    }

    /**
     * Everything still open to change, laid out as a settings bar.
     *
     * The panel above is what decides these answers, so the controls belong
     * where those numbers are. Options sit side by side rather than behind
     * submenus, because the whole point is comparing them: a merge level is
     * chosen by reading its mesh count against the others.
     */
    const answered = await menu(
      "What next?",
      [
        {
          id: "mergeMode",
          label: "Merge",
          value: options.mergeMode || "objects",
          options: MERGE_LEVELS.map((level) => {
            const n = preview.levels[level] ?? 0;
            return {
              value: level,
              label: `${level} (${num(n)} ${n === 1 ? "mesh" : "meshes"})`,
            };
          }),
        },
        {
          id: "textures",
          label: "Textures",
          value: wantTextures ? "include" : "skip",
          options: [
            { value: "include", label: "download" },
            { value: "skip", label: "skip" },
          ],
        },
        {
          id: "bones",
          label: "Skeleton",
          value: options.bones === false ? "drop" : "keep",
          options: [
            { value: "keep", label: "keep" },
            { value: "drop", label: "bake and drop" },
          ],
        },
        { id: "convert", kind: "confirm", label: "Convert" },
        { id: "cancel", kind: "cancel", label: "Cancel" },
      ],
      { enabled: !options.yes }
    );

    if (!answered) {
      console.log(`  ${c.gray("cancelled")}`);
      return null;
    }

    // Apply the settled choices, so the rest of the run reads one source.
    options.merge = answered.mergeMode !== "off";
    options.mergeMode = answered.mergeMode;
    options.downloadTextures = answered.textures === "include";
    options.bones = answered.bones !== "drop";
    console.log(`  ${c.gray(`merge=${answered.mergeMode} · textures=${answered.textures} · skeleton=${answered.bones}`)}`);
    console.log();
  }

  const animSpin = new Spinner("Fetching animation clips", { enabled: !quiet });
  Object.assign(decrypted, await fetchAnimations(info, (m) => detail(m)));
  animSpin.stop();

  let textures = [];
  // Maps that could not be fetched; reported in info.json so a partial rip is
  // distinguishable from a complete one.
  let textureFailures = [];
  if (!wantTextures) {
    step("Textures: skipped (--no-textures)");
  } else {
    const texProgress = new Progress("textures", 1, { enabled: !quiet });
    textures = await fetchTextures(uid, (m) => {
      detail(m);
      const match = /^Texture (\d+)\/(\d+):/.exec(String(m));
      if (match) {
        texProgress.total = Number(match[2]);
        texProgress.done = Number(match[1]) - 1;
        const label = String(m).replace(/^Texture \d+\/\d+: /, "").slice(0, 22);
        texProgress.setLabel(label);
        texProgress.tick();
      }
    }, { maxEdge: maxTextureEdge, concurrency });
    texProgress.finish();
    textureFailures = textures.failed || [];
    if (textures.length) textures = await packOrmMaps(viewerMaterials, textures, log, maxTextureEdge);
    for (const t of textures) decrypted[t.name] = t.data;
    const fromDec = textures.filter((t) => t.fromDescramble).length;
    detail(`${textures.length} texture(s) ready` + (fromDec ? ` (${fromDec} descrambled)` : ""));
    // Surfaced to the user: a missing map is a hole in the model.
    for (const f of textureFailures) step(`  ${c.yellow("missing texture")} ${f.name} (${f.error})`);
  }

  const convSpin = new Spinner("Converting to glTF", { enabled: !quiet });
  let glb = null, rig = null, gltfExternal = null;
  let geometryCount = 0, textureEmbedCount = 0, skippedGeometries = [];
  try {
    const result = await convertOsgjsToGlbFromFiles(
      decrypted, viewerMaterials, textures, viewerOrientation(info),
      options.merge === true, options.mergeMode, viewerLights(info),
      options.bones !== false
    );
    glb = result.glb;
    rig = result.rig || null;
    gltfExternal = result.gltfExternal || null;
    geometryCount = result.geometryCount;
    textureEmbedCount = result.textureEmbedCount || (result.json && result.json.images && result.json.images.length) || 0;
    convSpin.stop();
    skippedGeometries = result.skippedGeometries || [];
    for (const s of skippedGeometries) detail(`skipped ${s.name}: ${s.reason}`);
    if (rig && rig.bones) {
      if (options.bones === false) {
        // The source rig was used and discarded; reporting its bones here would
        // describe a skeleton the file does not contain.
        detail(`rig baked to rest pose (${rig.bones} source bones, ${rig.skins} skins)`);
      } else {
        detail(`rig: ${rig.skins} skins, ${rig.bones} bones, ${rig.skeletons} skeletons` +
          (rig.animations ? `, ${rig.animations} animations, ${rig.channels} channels` : ""));
      }
    }
    if (result.json && result.json.materials) {
      const imgName = (idx) => {
        if (idx == null || !result.json.images || !result.json.textures) return "none";
        const src = result.json.textures[idx] && result.json.textures[idx].source;
        const im = result.json.images[src];
        return (im && im.name) || "none";
      };
      for (const m of result.json.materials) {
        const p = m.pbrMetallicRoughness || {};
        const bi = p.baseColorTexture && p.baseColorTexture.index;
        const ni = m.normalTexture && m.normalTexture.index;
        // Two decimals: the exact float is useful to nobody reading a log.
        const f = (v) => (typeof v === "number" ? v.toFixed(2) : String(v));
        detail(`mat ${m.name}: metal=${f(p.metallicFactor)} rough=${f(p.roughnessFactor)} albedo=${imgName(bi)} normal=${imgName(ni)}`);
      }
    }
    if (!geometryCount) step(`  ${c.yellow("warning")} GLB has 0 meshes, the convert may be incomplete`);
  } catch (e) {
    convSpin.stop();
    step(`  ${c.yellow("convert failed")} ${e && e.message ? e.message : e}`);
  }

  // ---- assemble output files ----
  const files = [];
  const written = new Set();
  if (glb) files.push({ name: `${name}.glb`, data: glb });
  if (gltfExternal) {
    files.push({ name: "model.gltf", data: gltfExternal.jsonText });
    files.push({ name: "model.bin", data: gltfExternal.bin });
    written.add("model.gltf"); written.add("model.bin");
    if (wantTextures) {
      for (const alias of gltfExternal.textureAliases || []) { files.push({ name: alias.to, data: alias.data }); written.add(alias.to); }
    }
  }
  for (const [fname, data] of Object.entries(decrypted)) {
    if (written.has(fname)) continue;
    if (!wantTextures && /^textures\//i.test(fname)) continue;
    files.push({ name: fname, data });
    written.add(fname);
  }

  const missing = wantTextures ? textures.filter((t) => !t.fromCapture && !t.fromDescramble && !t.fromOrmPack) : [];
  if (missing.length) {
    const lines = ["Textures with no pk-descramble and no viewer-blit (public CDN, often striped):",
      ...missing.map((t) => `  - ${t.originalName || t.name} uid=${t.uid || "?"} ${t.width || "?"}x${t.height || "?"}`), ""];
    files.push({ name: "MISSING_BLIT.txt", data: new TextEncoder().encode(lines.join("\n")) });
  }

  const format = glb ? "glb+gltf+osgjs" : "osgjs";
  files.push({ name: "README.txt", data: new TextEncoder().encode(README_TXT) });
  files.push({
    name: "info.json",
    data: new TextEncoder().encode(JSON.stringify({
      uid, name: info.name || name, author, source_url: pageUrl,
      project: "https://github.com/seryi882/sketchfab-downloader-extension",
      format, geometry_count: geometryCount, skipped_geometries: skippedGeometries,
      textures_downloaded: textures.length, textures_skipped: !wantTextures,
      textures_failed: textureFailures.map((f) => f.name),
      max_texture_edge: maxTextureEdge || "original", textures_embedded_in_glb: textureEmbedCount,
      animation_count: (rig && rig.animations) || 0, skeleton_count: (rig && rig.skeletons) || 0,
      bone_count: (rig && rig.bones) || 0,
      rip_time_ms: Date.now() - t0,
      log: logLines,
    }, null, 2)),
  });

  return { files, name, author, uid, glb, geometryCount, textureEmbedCount, textures, rig, startedAt: t0 };
}

const README_TXT = [
  "Sketchfab model rip (sfrip)",
  "Core: seryi882/sketchfab-public-downloader (sketchfab-downloader-extension)",
  "",
  "Files:",
  "  <name>.glb        self-contained glTF binary (geometry + PBR textures + animations + rig)",
  "  textures/         loose PBR maps (baseColor / normal / ORM / …) for external-texture pipelines",
  "  model.gltf+bin    external glTF (references textures/)",
  "  file.osgjs, model_file.bin   decrypted raw scene (pre-conversion)",
  "  info.json         source metadata + rip summary + full log",
  "",
  "Load <name>.glb in Blender / three.js / Unreal / Unity / Godot as a normal glTF.",
  "Open-source attribution: see the project link in info.json.",
].join("\n");

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------
function parseArgs(argv) {
  const opts = {
    url: null, out: "./out", textures: true, maxEdge: 0, zip: false,
    concurrency: 3, merge: true, mergeMode: "objects", bones: true,
    quiet: false, help: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--out" || a === "-o") opts.out = argv[++i];
    else if (a === "--no-textures") opts.textures = false;
    else if (a === "--no-bones") opts.bones = false;
    else if (a === "--max-edge") opts.maxEdge = Number(argv[++i]);
    else if (a === "--zip") opts.zip = true;
    /**
     * `--merge` with no value keeps the historical meaning (merge by authored
     * object); `--merge=<level>` picks a granularity. `--no-merge` is the
     * explicit way to ask for one mesh per part, which `--merge=off` also does.
     */
    else if (a === "--merge") { opts.merge = true; opts.mergeMode = "objects"; }
    else if (a === "--no-merge") { opts.merge = false; opts.mergeMode = "off"; }
    else if (a.startsWith("--merge=")) {
      const level = a.slice("--merge=".length).toLowerCase();
      const valid = ["off", "objects", "group", "groups", "material", "materials", "all"];
      if (!valid.includes(level)) {
        throw new Error(`Unknown merge level: ${level} (expected ${valid.join(", ")})`);
      }
      opts.merge = level !== "off";
      opts.mergeMode = level === "group" ? "groups" : level === "material" ? "materials" : level;
    }
    else if (a === "-q" || a === "--quiet") opts.quiet = true;
    else if (a === "-v" || a === "--verbose") opts.verbose = true;
    else if (a === "-y" || a === "--yes") opts.yes = true;
    else if (a === "--no-color") process.env.NO_COLOR = "1";
    else if (a === "--concurrency") opts.concurrency = Number(argv[++i]);
    else if (a === "--help" || a === "-h") opts.help = true;
    else if (a === "--version" || a === "-V") opts.version = true;
    else if (!a.startsWith("-") && !opts.url) opts.url = a;
    else if (a.startsWith("-")) throw new Error(`Unknown option: ${a}`);
  }
  return opts;
}

const USAGE = `${c.bold("sfrip")} ${c.gray("— rip a Sketchfab model to a clean GLB")}

${c.bold("Usage")}
  sfrip ${c.cyan("<sketchfab-url-or-uid>")} [options]

${c.bold("Output")}
  ${c.gray("-o, --out <dir>")}       where to write (default ${c.gray("./out")})
  ${c.gray("--zip")}                also write a single .zip of the folder

${c.bold("Contents")}
  ${c.gray("--no-textures")}        skip texture download
  ${c.gray("--max-edge <n>")}       cap texture resolution: 2048 or 4096
  ${c.gray("--no-bones")}           bake the rest pose and drop the skeleton
  ${c.gray("--concurrency <n>")}    parallel texture downloads (default 3)

${c.bold("Merging")}
  ${c.gray("--merge[=<level>]")}    how finely to split the scene

      ${pad("off", 10)} ${c.gray("one mesh per part")}
      ${pad("objects", 10)} ${c.gray("one mesh per authored object")} ${c.gray("(default)")}
      ${pad("groups", 10)} ${c.gray("one mesh per top-level group")}
      ${pad("materials", 10)} ${c.gray("one mesh per material, scene-wide")}
      ${pad("all", 10)} ${c.gray("a single mesh")}

  Every run prints the mesh count for each level before downloading textures,
  so the trade-off is visible rather than guessed at. Skinned meshes and morph
  targets keep their own nodes at every level, so rigged models are unaffected.

${c.bold("Other")}
  ${c.gray("-y, --yes")}            skip the confirmation prompt
  ${c.gray("-v, --verbose")}        show download and conversion detail
  ${c.gray("-q, --quiet")}          only print errors; stdout is the GLB path
  ${c.gray("--no-color")}           plain text
  ${c.gray("-V, --version")}        show the version
  ${c.gray("-h, --help")}           show this

${c.bold("Example")}
  sfrip ${c.cyan("https://sketchfab.com/3d-models/foo-1234abcd")} --merge=groups
`;

async function main() {
  const argv = process.argv.slice(2);
  const opts = parseArgs(argv);
  if (opts.version) { console.log(`sfrip ${VERSION}`); return; }
  if (opts.help) { console.log(USAGE); return; }
  if (!opts.url) { console.error(USAGE); process.exitCode = 1; return; }

  const res = await rip(opts.url, opts.out, {
    downloadTextures: opts.textures,
    maxTextureEdge: opts.maxEdge,
    concurrency: opts.concurrency,
    merge: opts.merge,
    mergeMode: opts.mergeMode,
    bones: opts.bones,
    quiet: opts.quiet,
    verbose: opts.verbose,
    yes: opts.yes,
  });

  if (!res) {
    // The user declined at the prompt; nothing to write and nothing to report.
    return;
  }

  const dirName = `${res.name}-${res.uid.slice(0, 8)}`;
  const dir = path.resolve(opts.out, dirName);
  fs.mkdirSync(dir, { recursive: true });

  let totalBytes = 0;
  for (const f of res.files) {
    const rel = f.name.replace(/^[\\/]+/, "");
    const fp = path.join(dir, rel);
    fs.mkdirSync(path.dirname(fp), { recursive: true });
    fs.writeFileSync(fp, f.data);
    totalBytes += f.data.length;
  }

  if (opts.quiet) {
    // One machine-readable line, for scripts that only want the path.
    console.log(path.join(dir, `${res.name}.glb`));
  } else {
    const glbBytes = res.glb ? res.glb.length : 0;
    console.log(heading("done"));
    console.log(row("output", c.cyan(dir)));
    console.log(row("files", `${res.files.length} · ${bytes(totalBytes)}`));
    if (res.glb) {
      console.log(row("glb", `${num(res.geometryCount)} mesh(es) · ${bytes(glbBytes)} · ${res.textureEmbedCount} texture(s) embedded`));
    }
    if (res.rig && res.rig.bones && opts.bones !== false) {
      console.log(row("rig", `${res.rig.skins} skin(s) · ${num(res.rig.bones)} bone(s)`));
      if (res.rig.animations) console.log(row("animation", `${res.rig.animations} clip(s) · ${num(res.rig.channels)} channel(s)`));
    }
    if (opts.bones === false) {
      console.log(row("skeleton", c.gray(`baked to rest pose, dropped (${num((res.rig && res.rig.bones) || 0)} source bones)`)));
    }
    console.log(row("time", duration(Date.now() - res.startedAt)));
    console.log();
  }

  if (opts.zip) {
    const zip = await buildZip(res.files);
    const zipName = `${dirName}.zip`;
    const zipPath = path.join(path.resolve(opts.out), zipName);
    fs.writeFileSync(zipPath, zip);
    console.log(row("zip", `${c.cyan(zipPath)} · ${bytes(zip.length)}`));
  }
}

main().catch((e) => {
  console.error(`\n${c.red("✖")} ${e && e.stack ? e.stack : e}`);
  process.exitCode = 1;
});
