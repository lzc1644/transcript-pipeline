/** Browser terms are opt-in input to a NEW session, never a replacement dictionary. */
export function validateTerms(values: string[]): string[] {
  const terms = [...new Set(values.map(value => value.trim()).filter(Boolean))];
  if (terms.length > 50 || terms.some(term => [...term].length > 80)) throw new Error("术语最多50项，每项1–80字；请精简后保存");
  return terms;
}
export function parseTerms(text: string) { return validateTerms(text.split(/[\n,，]/)); }
