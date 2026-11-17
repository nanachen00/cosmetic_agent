import { NextResponse } from "next/server";
import { lstat, readFile } from "node:fs/promises";
import path from "node:path";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
let build: Promise<string | null> | undefined;

async function runningBuild(): Promise<string | null> {
  const selected = process.env.NEXT_DIST_DIR || ".next";
  if (!/^\.next(?:-[A-Za-z0-9][A-Za-z0-9_-]*)?$/.test(selected)) return null;
  try {
    // The selected build is validated above and is read only at runtime. Keep
    // Turbopack from tracing every sibling build, source file and public asset.
    const directory = path.join(/* turbopackIgnore: true */ process.cwd(), selected);
    const file = path.join(directory, "BUILD_ID");
    if ((await lstat(directory)).isSymbolicLink()) return null;
    const stat = await lstat(file);
    if (!stat.isFile() || stat.size > 129 || stat.isSymbolicLink()) return null;
    const id = (await readFile(file, "utf8")).trim();
    return /^[A-Za-z0-9_-]{1,128}$/.test(id) ? id : null;
  } catch { return null; }
}

export async function GET() {
  build ||= runningBuild();
  const buildId = await build;
  return NextResponse.json(buildId
    ? { status: "ok", service: "frontend", buildId }
    : { status: "unavailable", service: "frontend" }, {
    status: buildId ? 200 : 503,
    headers: { "Cache-Control": "no-store" },
  });
}
