import test from "node:test";
import assert from "node:assert/strict";
import { compile, isLocked } from "../filters.mjs";

const live = compile(
  ["youtube.com", "linkedin.com", "instagram.com", "nytimes.com/games", ".x.com", "seznamzpravy.cz"],
  ["music.youtube.com", "youtube.com/embed", "instagram.com/direct"],
);
const locked = (u, f = live) => isLocked(f, u);

test("a host filter covers the site and its subdomains only", () => {
  assert.ok(locked("https://www.linkedin.com/feed/"));
  assert.ok(locked("https://cz.linkedin.com/in/someone"));
  assert.ok(locked("https://seznamzpravy.cz/"));
  assert.ok(!locked("https://notlinkedin.com/"));
  assert.ok(!locked("https://linkedin.com.evil.example/"));
  assert.ok(!locked("https://www.seznam.cz/"));
});

test("allow entries win where they are more specific", () => {
  assert.ok(locked("https://www.youtube.com/watch?v=1"));
  assert.ok(!locked("https://music.youtube.com/watch?v=1"));
  assert.ok(!locked("https://www.youtube.com/embed/1"));
  assert.ok(!locked("https://www.instagram.com/direct/inbox/"));
  assert.ok(locked("https://www.instagram.com/"));
});

test("a path filter only locks that path", () => {
  assert.ok(locked("https://www.nytimes.com/games/wordle"));
  assert.ok(!locked("https://www.nytimes.com/section/world"));
  assert.ok(!locked("https://www.nytimes.com/Games/wordle"), "paths are case-sensitive");
});

test("a leading dot means this exact host", () => {
  assert.ok(locked("https://x.com/home"));
  assert.ok(!locked("https://api.x.com/home"));
  assert.ok(locked("https://a.b/", compile(["b", ".a.b"], [])));
});

test("precedence follows Chromium", () => {
  assert.ok(!locked("https://a.example/", compile(["a.example"], ["a.example"])), "allow wins a tie");
  assert.ok(locked("https://a.example/", compile([".a.example"], ["a.example"])), "exact host beats subdomain match");
  assert.ok(locked("https://a.example/x/y", compile(["a.example/x/y"], ["a.example/x"])), "longer path wins");
  assert.ok(!locked("https://a.example/", compile(["*"], ["a.example"])), "the wildcard is weakest");
  assert.ok(locked("https://b.example/", compile(["*"], ["a.example"])));
});

test("hosts ignore case and a trailing dot; schemes and ports are honoured", () => {
  assert.ok(locked("https://LinkedIn.com/"));
  assert.ok(locked("https://linkedin.com./feed"));
  assert.ok(locked("https://LINKEDIN.com/", compile(["LinkedIn.COM"], [])));
  assert.ok(!locked("http://a.example/", compile(["https://a.example"], [])));
  assert.ok(locked("https://a.example/", compile(["a.example:443"], [])));
  assert.ok(!locked("https://a.example:8443/", compile(["a.example:443"], [])));
});

test("allow filters with a query or an @ in the path still spare the page", () => {
  const f = compile(["youtube.com"], ["youtube.com/@chan", "youtube.com/watch?v=abc", "a.youtube.com/r?u=https://b"]);
  assert.ok(!locked("https://www.youtube.com/@chan/videos", f));
  assert.ok(!locked("https://www.youtube.com/watch?v=abc", f));
  assert.ok(!locked("https://a.youtube.com/r?u=https://b", f));
  assert.ok(locked("https://www.youtube.com/feed", f));
});

test("a block filter with a query is left to the browser", () => {
  assert.ok(!locked("https://a.example/x?y=1", compile(["a.example/x?y=1"], [])));
});

test("scheme and port do not count towards precedence", () => {
  assert.ok(!locked("https://a.example/", compile(["https://a.example"], ["a.example"])));
  assert.ok(!locked("https://a.example/", compile(["a.example:443"], ["a.example"])));
});

test("only web addresses can be locked", () => {
  for (const u of ["chrome://settings", "vivaldi://extensions", "chrome-extension://abc/blocked.html?u=https://linkedin.com", "about:blank", "file:///home/x", "", "not a url"]) {
    assert.ok(!locked(u), u);
  }
});

test("unsupported or malformed filters are ignored", () => {
  for (const bad of ["a.example/x@q=1", "*.example.com", "", 7, null]) {
    assert.ok(!locked("https://a.example/x", compile([bad], [])), String(bad));
  }
  assert.deepEqual(compile(undefined, undefined), []);
  assert.ok(!locked("https://linkedin.com/", compile([], [])));
});
