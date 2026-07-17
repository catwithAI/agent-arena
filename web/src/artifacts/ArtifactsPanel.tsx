// Office 文档预览面板（W7）：xlsx/pptx/docx 结构化预览 + 安全兜底。
// 已知简化：xlsx 大表格采用简单窗口化（按可视行数切片），非完整虚拟滚动库；
// 公式只展示不执行；外链只放行 http/https scheme。
import { useEffect, useMemo, useRef, useState } from "react";

import { api, type ArtifactPreviewDescriptor, type ArtifactStep } from "../api/client";

function artifactRef(step: string, name: string): string {
  return step === "." ? `./${name}` : `${step}/${name}`;
}

type SelectedFile = {
  step: string;
  name: string;
  size: number;
  type: string;
  media_type?: string;
};

// ── XLSX ──

type XlsxCell = {
  ref: string;
  row: number;
  column: number;
  value: unknown;
  display_value?: unknown;
  raw_value?: unknown;
  value_type?: string;
  formula?: string | null;
  number_format?: string | null;
};
type XlsxRow = { index: number; hidden: boolean; height: number | null; cells: XlsxCell[] };
type XlsxSheet = { name: string; dimension: string; rows: XlsxRow[]; truncated: boolean };

const ROW_HEIGHT = 28;
const WINDOW_ROWS = 40;

function cellText(cell: XlsxCell): string {
  if (cell.formula) return `=${cell.formula}`;
  const v = cell.display_value ?? cell.value;
  if (v === null || v === undefined) return "";
  return String(v);
}

function XlsxViewer({
  content,
  sheetIndex,
  onSheetChange,
}: {
  content: { sheets: XlsxSheet[]; formulas_evaluated: boolean; truncated: boolean };
  sheetIndex: number;
  onSheetChange: (i: number) => void;
}) {
  const [zoom, setZoom] = useState(100);
  const [search, setSearch] = useState("");
  const [scrollTop, setScrollTop] = useState(0);
  const sheets = content.sheets;
  const sheet = sheets[sheetIndex] ?? sheets[0];

  const rows = useMemo(() => {
    if (!sheet) return [];
    if (!search) return sheet.rows;
    const q = search.toLowerCase();
    return sheet.rows.filter((r) =>
      r.cells.some((c) => cellText(c).toLowerCase().includes(q)),
    );
  }, [sheet, search]);

  const totalHeight = rows.length * ROW_HEIGHT;
  const startIdx = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - 5);
  const visibleRows = rows.slice(startIdx, startIdx + WINDOW_ROWS);

  const maxCols = useMemo(
    () => (sheet ? sheet.rows.reduce((m, r) => Math.max(m, ...r.cells.map((c) => c.column)), 1) : 1),
    [sheet],
  );

  return (
    <div className="xlsx-viewer" style={{ fontSize: `${zoom}%` }}>
      <div className="wire-tabs" role="tablist">
        {sheets.map((s, i) => (
          <button key={s.name} role="tab" aria-selected={i === sheetIndex} onClick={() => onSheetChange(i)}>
            {s.name}
          </button>
        ))}
      </div>
      <p className="muted">公式只展示，不执行。</p>
      <div className="xlsx-toolbar">
        <input
          placeholder="值或公式"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <button onClick={() => setZoom((z) => Math.min(z + 10, 200))}>放大</button>
        <span>{zoom}%</span>
      </div>
      {rows.length === 0 ? (
        <p>没有匹配的单元格。</p>
      ) : (
        <div
          className="xlsx-grid-scroll"
          style={{ maxHeight: 400, overflowY: "auto" }}
          onScroll={(e) => setScrollTop((e.target as HTMLDivElement).scrollTop)}
        >
          <div style={{ height: totalHeight, position: "relative" }}>
            <table style={{ position: "absolute", top: startIdx * ROW_HEIGHT }}>
              <tbody>
                {visibleRows.map((row) => (
                  <tr key={row.index} style={{ height: ROW_HEIGHT }}>
                    <td className="muted">{row.index}</td>
                    {Array.from({ length: maxCols }, (_, i) => i + 1).map((col) => {
                      const cell = row.cells.find((c) => c.column === col);
                      return <td key={col}>{cell ? cellText(cell) : ""}</td>;
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ── PPTX ──

type PptxElement = { kind: string; x: number; y: number; width: number; height: number; text?: string; role?: string };
type PptxSlide = { number: number; elements: PptxElement[] };
type PptxContent = {
  width: number; height: number; aspect_ratio: number;
  slides: PptxSlide[];
};

function PptxViewer({
  content,
  page,
  onPageChange,
  syncEnabled,
  syncPage,
  setSyncPage,
}: {
  content: PptxContent;
  page: number;
  onPageChange: (p: number) => void;
  syncEnabled: boolean;
  syncPage: number;
  setSyncPage: (p: number) => void;
}) {
  const [zoom, setZoom] = useState(100);
  const slides = content.slides;
  const effectivePage = syncEnabled ? syncPage : page;
  const outOfRange = effectivePage >= slides.length;
  const slide = slides[Math.min(effectivePage, slides.length - 1)];

  function goto(next: number) {
    if (syncEnabled) setSyncPage(next);
    else onPageChange(next);
  }

  return (
    <div className="pptx-viewer" aria-label="幻灯片预览">
      <div style={{ fontSize: `${zoom}%`, position: "relative", aspectRatio: content.aspect_ratio }}>
        {outOfRange ? (
          <p className="muted">同步页 {effectivePage + 1} 在此演示文稿中不存在。</p>
        ) : (
          slide?.elements.map((el, i) => (
            <div
              key={i}
              style={{
                position: "absolute",
                left: `${el.x * 100}%`,
                top: `${el.y * 100}%`,
                width: `${el.width * 100}%`,
                height: `${el.height * 100}%`,
              }}
            >
              {el.kind === "text" && el.text}
            </div>
          ))
        )}
      </div>
      <div className="pptx-toolbar">
        <button onClick={() => goto(Math.max(0, effectivePage - 1))}>上一页</button>
        <span>{Math.min(effectivePage, slides.length - 1) + 1} / {slides.length}</span>
        <button onClick={() => goto(effectivePage + 1)}>下一页</button>
        <button onClick={() => setZoom((z) => Math.min(z + 10, 200))}>放大</button>
        <span>{zoom}%</span>
      </div>
    </div>
  );
}

// ── DOCX ──

type DocxRun = { text: string; bold?: boolean; href?: string };
type DocxBlock =
  | { kind: "paragraph"; text: string; heading: number | null; list_item: boolean; runs: DocxRun[] }
  | { kind: "table"; rows: Array<Array<{ text: string; blocks: DocxBlock[] }>> };
type DocxContent = { blocks: DocxBlock[]; external_links: number; images_omitted: number };

function safeHref(href: string | undefined): string | null {
  if (!href) return null;
  try {
    const url = new URL(href, "https://placeholder.invalid/");
    if (href.startsWith("http://") || href.startsWith("https://")) return href;
    if (url.protocol === "http:" || url.protocol === "https:") return href;
    return null;
  } catch {
    return null;
  }
}

function DocxRunView({ run }: { run: DocxRun }) {
  const href = safeHref(run.href);
  if (href) return <a href={href}>{run.text}</a>;
  return <span style={run.bold ? { fontWeight: "bold" } : undefined}>{run.text}</span>;
}

function DocxBlockView({ block }: { block: DocxBlock }) {
  if (block.kind === "table") {
    return (
      <table className="docx-table">
        <tbody>
          {block.rows.map((row, ri) => (
            <tr key={ri}>
              {row.map((cell, ci) => (
                <td key={ci}>{cell.text}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  if (block.heading) {
    const Tag = `h${Math.min(block.heading + 1, 6)}` as keyof JSX.IntrinsicElements;
    return <Tag>{block.runs.map((r, i) => <DocxRunView key={i} run={r} />)}</Tag>;
  }
  return <p>{block.runs.map((r, i) => <DocxRunView key={i} run={r} />)}</p>;
}

function DocxViewer({ content }: { content: DocxContent }) {
  const [search, setSearch] = useState("");
  const blocks = content.blocks;
  const matchCount = useMemo(() => {
    if (!search) return null;
    const q = search.toLowerCase();
    return blocks.filter((b) => b.kind === "paragraph" && b.text.toLowerCase().includes(q)).length;
  }, [blocks, search]);

  return (
    <div className="docx-viewer">
      <input placeholder="文档文字" value={search} onChange={(e) => setSearch(e.target.value)} />
      {matchCount !== null && <p>{matchCount} 个匹配块</p>}
      {blocks.map((b, i) => (
        <DocxBlockView key={i} block={b} />
      ))}
    </div>
  );
}

// ── 预览 shell：根据 descriptor.status 分流 ──

function PreviewBody({
  descriptor,
  runId,
  attemptId,
  artifactRef: fileRef,
  sync,
}: {
  descriptor: ArtifactPreviewDescriptor;
  runId: string;
  attemptId: string;
  artifactRef: string;
  sync?: OfficeSync;
}) {
  const [localSheet, setLocalSheet] = useState(0);
  const [localPage, setLocalPage] = useState(0);

  const downloadLink = (
    <a href={api.artifactUrl(runId, attemptId, fileRef)}>下载原文件</a>
  );

  if (descriptor.status === "unsupported" || descriptor.status === "failed") {
    const counts = descriptor.counts ?? {};
    return (
      <div>
        <p>内容预览尚未接入</p>
        {typeof counts.slides === "number" && <p>{counts.slides} 页幻灯片</p>}
        {typeof counts.sheets === "number" && <p>{counts.sheets} 个工作表</p>}
        {typeof counts.pages === "number" && <p>{counts.pages} 页</p>}
        {descriptor.error?.message && <p className="muted">{descriptor.error.message}</p>}
        {descriptor.capability_gaps.length > 0 && (
          <p className="muted">{descriptor.capability_gaps.join(", ")}</p>
        )}
        <p>{downloadLink}</p>
      </div>
    );
  }

  if (descriptor.status === "rendering") {
    return <p>正在后台生成预览…</p>;
  }

  const content = descriptor.content as Record<string, unknown> | undefined;
  if (!content) {
    return <p className="muted">没有可显示的内容。</p>;
  }
  const kind = content.kind;
  if (kind === "workbook") {
    const sheets = (content.sheets as XlsxSheet[]) ?? [];
    if (sheets.length === 0) return <p>工作簿没有可显示的工作表。</p>;
    return (
      <XlsxViewer
        content={content as never}
        sheetIndex={Math.min(localSheet, sheets.length - 1)}
        onSheetChange={setLocalSheet}
      />
    );
  }
  if (kind === "presentation") {
    return (
      <PptxViewer
        content={content as never}
        page={localPage}
        onPageChange={setLocalPage}
        syncEnabled={Boolean(sync?.enabled)}
        syncPage={sync?.page ?? 0}
        setSyncPage={(p) => sync?.setPage(p)}
      />
    );
  }
  if (kind === "document") {
    return <DocxViewer content={content as never} />;
  }
  return <p className="muted">未知预览类型。</p>;
}

function FilePreview({
  runId,
  attemptId,
  step,
  file,
  officeSync,
}: {
  runId: string;
  attemptId: string;
  step: string;
  file: SelectedFile;
  officeSync?: OfficeSync;
}) {
  const [descriptor, setDescriptor] = useState<ArtifactPreviewDescriptor | null>(null);
  const timerRef = useRef<number | null>(null);
  const ref = artifactRef(step, file.name);

  useEffect(() => {
    setDescriptor(null);
    const controller = new AbortController();
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function poll() {
      try {
        const d = await api.getArtifactPreview(runId, attemptId, ref, controller.signal);
        if (cancelled) return;
        setDescriptor(d);
        if (d.status === "rendering" && d.poll_after_ms) {
          timer = setTimeout(poll, d.poll_after_ms);
        }
      } catch {
        // aborted or failed request — swallow; stale requests are expected to be cancelled.
      }
    }
    poll();

    return () => {
      cancelled = true;
      controller.abort();
      if (timer) clearTimeout(timer);
      if (timerRef.current) clearTimeout(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, attemptId, ref]);

  if (!descriptor) return <p>正在安全检查预览…</p>;

  return (
    <div>
      <PreviewBody descriptor={descriptor} runId={runId} attemptId={attemptId} artifactRef={ref} sync={officeSync} />
      {descriptor.capability_gaps.length > 0 && descriptor.status === "ready" && (
        <p role="note">{descriptor.capability_gaps.join(", ")}</p>
      )}
    </div>
  );
}

export type OfficeSync = {
  enabled: boolean;
  page: number;
  sheet: number;
  setPage: (p: number) => void;
  setSheet: (s: number) => void;
};

export function ArtifactsPanel({
  runId,
  attemptId,
  steps,
  officeSync,
}: {
  runId: string;
  attemptId: string;
  steps: ArtifactStep[];
  officeSync?: OfficeSync;
}) {
  const [openStep, setOpenStep] = useState<string | null>(null);
  const [selected, setSelected] = useState<{ step: string; file: SelectedFile } | null>(null);

  return (
    <div className="artifacts-panel">
      {steps.length === 0 && <p className="muted">暂无产物文件。</p>}
      {steps.map((s) => (
        <div key={s.step}>
          <p className="muted" onClick={() => setOpenStep(openStep === s.step ? null : s.step)} style={{ cursor: "pointer" }}>
            {s.step}
          </p>
          {openStep === s.step && (
            <ul>
              {s.files.map((f) => (
                <li key={f.name}>
                  <button onClick={() => setSelected({ step: s.step, file: f as SelectedFile })}>{f.name}</button>{" "}
                  <span className="muted">({f.size}B)</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
      {selected && (
        <FilePreview
          key={`${selected.step}/${selected.file.name}`}
          runId={runId}
          attemptId={attemptId}
          step={selected.step}
          file={selected.file}
          officeSync={officeSync}
        />
      )}
    </div>
  );
}
