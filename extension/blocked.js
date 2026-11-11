import { compile, isLocked } from "./filters.mjs";

const target = new URLSearchParams(location.search).get("u") || "";
let url = null;
try {
  const parsed = new URL(target);
  if (parsed.protocol === "http:" || parsed.protocol === "https:") url = parsed;
} catch {
  // Leave the generic heading.
}

if (url) {
  document.getElementById("site").textContent = url.hostname.replace(/^www\./, "");
  document.title = `${url.hostname.replace(/^www\./, "")} is locked`;
}

// The history entry right behind this page is the locked address, and going
// there only lands back here, so step over it.
const back = document.getElementById("back");
if (history.length > 2) {
  back.hidden = false;
  back.addEventListener("click", () => history.go(-2));
}

async function returnIfUnlocked() {
  if (!url) return;
  const { block, allow } = await chrome.storage.managed.get(["block", "allow"]);
  if (Array.isArray(block) && !isLocked(compile(block, allow), url.href)) location.replace(url.href);
}

chrome.storage.onChanged.addListener((_changes, area) => {
  if (area === "managed") returnIfUnlocked();
});
returnIfUnlocked();
