import { describe, expect, it } from "vitest";
import { HERMES_DOCS_URL, docsUrlForLocale } from "@/lib/docs-url";

describe("docsUrlForLocale", () => {
  it("keeps English documentation embedded at its canonical URL", () => {
    expect(docsUrlForLocale("en")).toBe(HERMES_DOCS_URL);
  });

  it("offers Portuguese readers an automatic translation", () => {
    const url = new URL(docsUrlForLocale("pt"));

    expect(url.origin).toBe("https://translate.google.com");
    expect(url.searchParams.get("sl")).toBe("en");
    expect(url.searchParams.get("tl")).toBe("pt");
    expect(url.searchParams.get("u")).toBe(HERMES_DOCS_URL);
  });
});
