// Decides whether a URL is locked, using the same filter format and the same
// precedence as Chromium's URLBlocklist/URLAllowlist policies, so a tab is only
// replaced when the browser itself would refuse to load that address.
//
// Supported: [scheme://][.]host[:port][/path], "*" as the host.  A wildcard
// inside the host is invalid in Chromium too.  A query part can't be checked
// here: a block filter with one is left to the browser, which enforces it on the
// next page load, and an allow filter is matched without it, because replacing a
// page the browser permits is the worse mistake.

function parse(text, allow) {
  if (typeof text !== "string") return null;
  let rest = text.trim();
  const query = rest.indexOf("?");
  if (query >= 0) {
    if (!allow) return null;
    rest = rest.slice(0, query);
  }
  if (!rest) return null;
  let scheme = null;
  const sep = rest.indexOf("://");
  if (sep >= 0 && !rest.slice(0, sep).includes("/")) {
    scheme = rest.slice(0, sep).toLowerCase();
    rest = rest.slice(sep + 3);
  }
  const slash = rest.indexOf("/");
  const path = slash >= 0 ? rest.slice(slash) : "";
  let host = (slash >= 0 ? rest.slice(0, slash) : rest).toLowerCase();
  if (host.includes("@")) return null;
  let port = null;
  const colon = host.lastIndexOf(":");
  if (colon >= 0) {
    port = host.slice(colon + 1);
    host = host.slice(0, colon);
  }
  const exact = host.startsWith(".");
  if (exact) host = host.slice(1);
  const wildcard = host === "*";
  if (wildcard) host = "";
  else if (!host || host.includes("*")) return null;
  return { scheme, host, port, path, exact, wildcard, allow };
}

export function compile(block, allow) {
  const list = (xs, isAllow) =>
    (Array.isArray(xs) ? xs : []).map((x) => parse(x, isAllow)).filter(Boolean);
  return [...list(block, false), ...list(allow, true)];
}

function matches(f, url) {
  if (f.scheme && f.scheme !== url.protocol.slice(0, -1)) return false;
  if (f.port && f.port !== (url.port || (url.protocol === "https:" ? "443" : "80"))) return false;
  const h = url.hostname.toLowerCase().replace(/\.$/, "");
  if (!f.wildcard) {
    if (f.exact) {
      if (h !== f.host) return false;
    } else if (h !== f.host && !h.endsWith("." + f.host)) {
      return false;
    }
  }
  return !f.path || url.pathname.startsWith(f.path);
}

// True if a takes precedence over b (Chromium's FilterTakesPrecedence).
function beats(a, b) {
  if (a.wildcard !== b.wildcard) return b.wildcard;
  if (a.exact !== b.exact) return a.exact;
  if (a.host.length !== b.host.length) return a.host.length > b.host.length;
  if (a.path.length !== b.path.length) return a.path.length > b.path.length;
  return a.allow && !b.allow;
}

export function isLocked(filters, address) {
  let url;
  try {
    url = new URL(address);
  } catch {
    return false;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return false;
  let best = null;
  for (const f of filters) {
    if (matches(f, url) && (best === null || beats(f, best))) best = f;
  }
  return best !== null && !best.allow;
}
