"use strict";

// Format of the text that the "Copiar" button puts on the clipboard.
// Change these values to match the app you paste into.
const COPY_FORMAT = {
  separator: " | ",         // between columns
  decimalSeparator: ",",    // "314,3" or "314.3"
  header: true,             // first line: Ingrediente | Cantidad | Kcal | Prot | Carbs | Grasas
  total: true,              // last line: TOTAL
  notFound: "no encontrado" // text in the value columns of a row without data
};
const COLUMNS = ["Ingrediente", "Cantidad", "Kcal", "Prot", "Carbs", "Grasas"];
const MACROS = ["kcal", "prot", "carbs", "grasas"];

const $ = (id) => document.getElementById(id);
const form = $("form");
const texto = $("texto");
const boton = $("calcular");
let ultimo = null;

// ---------------------------------------------------------------- formatting
function num(x) {
  // The backend already rounds to 1 decimal. Only the separator changes here.
  return x.toFixed(1).replace(".", COPY_FORMAT.decimalSeparator);
}

function cantidad(gramos) {
  const r = Math.round(gramos * 10) / 10;
  const s = Number.isInteger(r) ? String(r) : r.toFixed(1).replace(".", COPY_FORMAT.decimalSeparator);
  return s + " g";
}

function filaTexto(fila) {
  const cols = [fila.ingrediente, cantidad(fila.gramos)];
  if (fila.encontrado) cols.push(...MACROS.map((m) => num(fila.redondeado[m])));
  else cols.push(COPY_FORMAT.notFound, "", "", "");
  return cols;
}

function textoCopia(res) {
  const lineas = [];
  if (COPY_FORMAT.header) lineas.push(COLUMNS.join(COPY_FORMAT.separator));
  for (const f of res.filas) lineas.push(filaTexto(f).join(COPY_FORMAT.separator));
  if (COPY_FORMAT.total) {
    lineas.push(["TOTAL", "", ...MACROS.map((m) => num(res.total_redondeado[m]))].join(COPY_FORMAT.separator));
  }
  return lineas.join("\n");
}

// ---------------------------------------------------------------- rendering
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

function enlace(texto, url) {
  return url ? el("a", { href: url, target: "_blank", rel: "noopener" }, texto) : texto;
}

function describirOrigen(o) {
  if (!o) return "";
  if (o.tipo === "Usuario" && !o.complemento) return "valores de la etiqueta (usuario)";
  const fuente = o.tipo === "Usuario" ? `etiqueta + ${o.complemento}` : o.tipo;
  const marca = o.marca ? ` (${o.marca})` : "";
  return `${fuente}: ${o.nombre}${marca}`;
}

function pintar(res) {
  const tbody = $("tabla").querySelector("tbody");
  const tfoot = $("tabla").querySelector("tfoot");
  tbody.replaceChildren();
  tfoot.replaceChildren();

  for (const f of res.filas) {
    const tr = el("tr", { class: [!f.encontrado && "no-encontrado", f.aviso && f.encontrado && "con-aviso"].filter(Boolean).join(" ") });
    const nombre = el("td", { class: "ingrediente" }, f.ingrediente);
    // Sensitivity: another plausible variant changes the kcal a lot.
    if (f.encontrado && f.sensibilidad) nombre.append(el("div", { class: "sensibilidad" }, f.sensibilidad));
    tr.append(nombre, el("td", { class: "num" }, cantidad(f.gramos)));
    if (f.encontrado) {
      for (const m of MACROS) tr.append(el("td", { class: "num" }, num(f.redondeado[m])));
    } else {
      tr.append(el("td", { class: "num", colspan: "4" }, "no encontrado"));
    }
    tbody.append(tr);
  }
  const total = el("tr", {}, el("td", {}, "TOTAL"), el("td", {}, ""));
  for (const m of MACROS) total.append(el("td", { class: "num" }, num(res.total_redondeado[m])));
  tfoot.append(total);

  const avisos = $("avisos");
  avisos.replaceChildren();
  for (const a of res.avisos) avisos.append(el("p", {}, a));
  for (const f of res.filas) if (f.aviso && f.encontrado) avisos.append(el("p", {}, `${f.ingrediente}: ${f.aviso}`));

  const detalles = $("detalles");
  detalles.replaceChildren();
  const fuentes = el("ul");
  for (const f of res.filas) {
    const origen = f.encontrado ? enlace(describirOrigen(f.origen), f.origen && f.origen.url) : "no encontrado (no se estima)";
    const li = el("li", {}, el("span", { class: "fuente" }, f.ingrediente + ": "), origen);
    if (f.motivo) li.append(el("span", {}, ` — ${f.motivo}`));
    if (f.alternativa && f.sensibilidad) {
      li.append(el("br"), el("span", {}, "Alternativa: "), enlace(describirOrigen(f.alternativa), f.alternativa.url));
    }
    fuentes.append(li);
  }
  detalles.append(el("h2", {}, "Fuente de cada fila"), fuentes);

  const supuestos = res.filas.flatMap((f) => f.supuestos.map((s) => `${f.ingrediente}: ${s}`));
  if (supuestos.length) {
    const ul = el("ul");
    for (const s of supuestos) ul.append(el("li", {}, s));
    detalles.append(el("h2", {}, "Supuestos"), ul);
  }

  $("resultado").hidden = false;
}

// ---------------------------------------------------------------- clipboard
function copiarFallback(text) {
  // navigator.clipboard needs HTTPS or localhost. Over plain HTTP on the LAN
  // (a phone that opens http://192.168.x.x:8000) use a hidden textarea.
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.top = "0";
  ta.style.left = "0";
  ta.style.opacity = "0";
  ta.style.fontSize = "16px"; // stops the zoom on iOS
  document.body.append(ta);
  ta.focus();
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}

async function copiar(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* use the fallback */ }
  }
  return copiarFallback(text);
}

$("copiar").addEventListener("click", async () => {
  if (!ultimo) return;
  const ok = await copiar(textoCopia(ultimo));
  const aviso = $("copiado");
  aviso.textContent = ok ? "Copiado" : "No se pudo copiar: selecciona la tabla a mano";
  aviso.hidden = false;
  clearTimeout(aviso._t);
  aviso._t = setTimeout(() => { aviso.hidden = true; }, 2500);
});

// ---------------------------------------------------------------- request
function mostrarError(msg) {
  const e = $("error");
  e.textContent = msg;
  e.hidden = !msg;
}

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const valor = texto.value.trim();
  if (!valor) return;
  mostrarError("");
  boton.disabled = true;
  $("cargando").hidden = false;
  const t0 = Date.now();
  const reloj = setInterval(() => { $("segundos").textContent = Math.round((Date.now() - t0) / 1000); }, 500);
  $("segundos").textContent = "0";
  try {
    const resp = await fetch("api/calcular", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texto: valor }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const detalle = typeof data.detail === "string" ? data.detail : `Error ${resp.status}`;
      throw new Error(detalle);
    }
    ultimo = data;
    pintar(data);
    $("resultado").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    mostrarError(err.message || "Error de red");
  } finally {
    clearInterval(reloj);
    boton.disabled = false;
    $("cargando").hidden = true;
  }
});

texto.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) form.requestSubmit();
});
texto.addEventListener("input", () => {
  $("contador").textContent = `${texto.value.length} / ${texto.maxLength}`;
});

// ---------------------------------------------------------------- footer data
function fecha(iso) {
  return iso ? String(iso).slice(0, 10) : "?";
}

fetch("api/info")
  .then((r) => r.json())
  .then((info) => {
    if (info.max_chars) {
      texto.maxLength = info.max_chars;
      $("contador").textContent = `${texto.value.length} / ${info.max_chars}`;
    }
    const d = Object.fromEntries((info.datos || []).map((m) => [m.fuente, m]));
    const off = d["Open Food Facts"];
    const partes = [];
    if (off) partes.push(`Datos de Open Food Facts del ${fecha(off.fecha_descarga)} (${off.filtro_pais})`);
    const usda = Object.values(d).filter((m) => m.fuente.startsWith("USDA"));
    if (usda.length) partes.push("USDA FoodData Central: " + usda.map((m) => `${m.fuente.replace("USDA ", "")} ${m.publicacion}`).join(", "));
    $("fechas").textContent = partes.length ? partes.join(" · ") : "Sin base de datos: ejecuta el ETL.";
  })
  .catch(() => { $("fechas").textContent = "Datos: no disponibles"; });
