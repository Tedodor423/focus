// Replaces open tabs the moment their site locks.  The address lists arrive
// through managed storage: focusd writes them into the same browser policy file
// as URLBlocklist, so both change together.
import { compile, isLocked } from "./filters.mjs";

const BLOCK_PAGE = chrome.runtime.getURL("blocked.html");

async function currentFilters() {
  const { block, allow } = await chrome.storage.managed.get(["block", "allow"]);
  return compile(block, allow);
}

async function guard(tabId, address, filters) {
  if (!address || !isLocked(filters, address)) return;
  const url = `${BLOCK_PAGE}?u=${encodeURIComponent(address)}`;
  try {
    await chrome.tabs.update(tabId, { url });
  } catch {
    // Tab closed, or being dragged; the next sweep picks it up.
  }
}

async function sweep() {
  const [filters, tabs] = await Promise.all([currentFilters(), chrome.tabs.query({})]);
  await Promise.all(tabs.map((t) => guard(t.id, t.pendingUrl || t.url, filters)));
}

chrome.storage.onChanged.addListener((_changes, area) => {
  if (area === "managed") sweep();
});

// Sites that change page without a real load (LinkedIn, YouTube, Instagram)
// never reach the browser's own URL check, but they do show up here.
chrome.tabs.onUpdated.addListener(async (tabId, change) => {
  if (change.url) guard(tabId, change.url, await currentFilters());
});

chrome.runtime.onStartup.addListener(sweep);
chrome.runtime.onInstalled.addListener(sweep);

// Safety net in case a policy-change event is missed.  Checked first, because
// creating an alarm restarts its clock and this code runs on every wake-up.
chrome.alarms.get("sweep").then((alarm) => {
  if (!alarm) chrome.alarms.create("sweep", { periodInMinutes: 1 });
});
chrome.alarms.onAlarm.addListener(sweep);
