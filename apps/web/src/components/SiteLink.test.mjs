import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);

function transpile(path) {
  const source = readFileSync(new URL(path, import.meta.url), "utf8");
  return ts.transpileModule(source, {
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
}

const siteCode = transpile("../lib/site.ts");
const linkCode = transpile("../components/SiteLink.tsx");

function loadLink(mode) {
  const previous = process.env.NEXT_PUBLIC_SITE_MODE;
  process.env.NEXT_PUBLIC_SITE_MODE = mode;
  try {
    const site = { exports: {} };
    new Function("require", "module", "exports", siteCode)(require, site, site.exports);
    const link = { exports: {} };
    const mockedRequire = (name) => {
      if (name === "@/lib/site") return site.exports;
      if (name === "next/link") {
        return {
          __esModule: true,
          default: (props) => React.createElement("a", { ...props, "data-next-link": "true" }),
        };
      }
      return require(name);
    };
    new Function("require", "module", "exports", linkCode)(mockedRequire, link, link.exports);
    return link.exports.default;
  } finally {
    if (previous === undefined) delete process.env.NEXT_PUBLIC_SITE_MODE;
    else process.env.NEXT_PUBLIC_SITE_MODE = previous;
  }
}

test("static navigation targets exported HTML and preserves query and fragment", () => {
  const SiteLink = loadLink("static");
  const html = renderToStaticMarkup(React.createElement(SiteLink, {
    href: "/papers?id=paper-1#evidence",
    className: "workspace-link",
  }, "论文库"));
  assert.match(html, /href="\/papers\.html\?id=paper-1#evidence"/);
  assert.match(html, /class="workspace-link"/);
  assert.ok(!html.includes("data-next-link"));
});

test("static navigation leaves root, files, hashes, and external URLs unchanged", () => {
  const SiteLink = loadLink("static");
  for (const href of ["/", "/examples/lenet/report.md", "#method", "https://example.test/paper"]) {
    const html = renderToStaticMarkup(React.createElement(SiteLink, { href }, "target"));
    assert.ok(html.includes(`href="${href}"`), href);
  }
});

test("live navigation retains Next.js client-side links", () => {
  const SiteLink = loadLink("live");
  const html = renderToStaticMarkup(React.createElement(SiteLink, { href: "/papers" }, "论文库"));
  assert.match(html, /href="\/papers"/);
  assert.match(html, /data-next-link="true"/);
  assert.ok(!html.includes("papers.html"));
});
