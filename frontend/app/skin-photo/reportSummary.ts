export type Finding = { tag: string; region: string; evidence: string };
export type FindingGroup = { tag: string; regions: string[]; findings: Finding[] };

const regionOrder = ["forehead", "cheek", "nose", "chin", "eye_area", "unknown"];
const normalize = (value: string) => value.replace(/[\s，。；、,.!?！？：:]/g, "");

/** Group presentation only: retain every distinct region/evidence association. */
export function groupFindings(findings: Finding[]): FindingGroup[] {
  const groups = new Map<string, FindingGroup>();
  const seen = new Set<string>();
  for (const finding of findings) {
    const key = JSON.stringify([finding.tag, finding.region, normalize(finding.evidence)]);
    if (seen.has(key)) continue;
    seen.add(key);
    let group = groups.get(finding.tag);
    if (!group) {
      group = { tag: finding.tag, regions: [], findings: [] };
      groups.set(finding.tag, group);
    }
    if (!group.regions.includes(finding.region)) group.regions.push(finding.region);
    group.findings.push(finding);
  }
  return [...groups.values()].map(group => ({
    ...group,
    regions: [...group.regions].sort((a, b) => regionOrder.indexOf(a) - regionOrder.indexOf(b)),
  }));
}

export function uniqueStatements(statements: string[]): string[] {
  const seen = new Set<string>();
  return statements.filter(statement => {
    const key = normalize(statement);
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
