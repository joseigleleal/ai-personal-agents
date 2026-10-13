// Precargado en el proceso Node de Playwright (NODE_OPTIONS=--require): resuelve los hosts de
// CITA_FIJAR_IP ("host=ip,host=ip") a esa IP. Hace falta porque con client_certificates Playwright
// abre las conexiones desde Node y no respeta --host-resolver-rules de Chromium.
const dns = require("dns");
const mapa = Object.fromEntries((process.env.CITA_FIJAR_IP || "").split(",").filter(Boolean)
  .map((par) => par.split("=")));
const fijada = (host, opciones = {}) => {
  const familia = typeof opciones === "number" ? opciones : opciones.family;
  if (familia === 6) return opciones.all ? [] : null;
  const ip = { address: mapa[host], family: 4 };
  return opciones.all ? [ip] : ip;
};

const lookup = dns.lookup;
dns.lookup = function (host, opciones, cb) {
  if (!(host in mapa)) return lookup.apply(this, arguments);
  if (typeof opciones === "function") [cb, opciones] = [opciones, {}];
  const r = fijada(host, opciones);
  if (r === null) return process.nextTick(cb, Object.assign(new Error("sin IPv6"), { code: "ENOTFOUND" }));
  if (Array.isArray(r)) return process.nextTick(cb, null, r);
  process.nextTick(cb, null, r.address, r.family);
};

const lookupPromesa = dns.promises.lookup;
dns.promises.lookup = async function (host, opciones) {
  if (!(host in mapa)) return lookupPromesa.apply(this, arguments);
  const r = fijada(host, opciones);
  if (r === null) throw Object.assign(new Error("sin IPv6"), { code: "ENOTFOUND" });
  return r;
};
