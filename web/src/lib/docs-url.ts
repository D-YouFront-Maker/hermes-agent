import type { Locale } from "@/i18n/types";

export const HERMES_DOCS_URL = "https://hermes-agent.nousresearch.com/docs/";

export function docsUrlForLocale(locale: Locale): string {
  if (locale !== "pt") return HERMES_DOCS_URL;

  const translated = new URL("https://translate.google.com/translate");
  translated.searchParams.set("sl", "en");
  translated.searchParams.set("tl", "pt");
  translated.searchParams.set("u", HERMES_DOCS_URL);
  return translated.toString();
}
