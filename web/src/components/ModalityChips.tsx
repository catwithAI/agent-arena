/** Modality badges for models / envs (OpenRouter MODALITIES style).
 *
 * OpenRouter renders a group of input-modality icons followed by a group of
 * output icons: text = blue T, image = green picture, audio = pink
 * headphones, video = red camera. `ModalityBadges` renders the full
 * "input → output" row, `ModalityChip` renders a single badge.
 */

const MODALITY_META: Record<string, { label: string; icon: string; cls: string }> = {
  text: { label: "文本", icon: "T", cls: "mod-text" },
  image: { label: "图片", icon: "🖼", cls: "mod-image" },
  audio: { label: "音频", icon: "🎧", cls: "mod-audio" },
  video: { label: "视频", icon: "🎬", cls: "mod-video" },
  file: { label: "文件", icon: "📄", cls: "mod-file" },
};

export function ModalityChip({ modality }: { modality: string }) {
  const meta = MODALITY_META[modality] ?? {
    label: modality,
    icon: modality[0]?.toUpperCase() ?? "?",
    cls: "mod-other",
  };
  return (
    <span className={`mod-chip ${meta.cls}`} title={`${meta.label}（${modality}）`}>
      {meta.icon}
    </span>
  );
}

export function ModalityBadges({
  input, output,
}: {
  input?: string[] | null;
  output?: string[] | null;
}) {
  const ins = input ?? [];
  const outs = output ?? [];
  if (ins.length === 0 && outs.length === 0) return null;
  return (
    <span className="mod-badges" aria-label="modalities">
      {ins.map((m) => <ModalityChip key={`in-${m}`} modality={m} />)}
      {outs.length > 0 && <span className="mod-arrow">→</span>}
      {outs.map((m) => <ModalityChip key={`out-${m}`} modality={m} />)}
    </span>
  );
}

/** Cross-check of the env's declared agent-side modality requirements
 * against the selected model's input_modalities. Returns the missing
 * modalities (empty = compatible or undecidable). */
export function missingModalities(
  required?: string[] | null,
  modelInput?: string[] | null,
): string[] {
  if (!required || required.length === 0) return [];
  // Model modality data missing (e.g. a hand-typed non-OpenRouter model id)
  // means we can't judge — never emit a false warning.
  if (!modelInput || modelInput.length === 0) return [];
  return required.filter((m) => !modelInput.includes(m));
}

/** Native <option> elements can't hold components — mark the env dropdown's
 * modality requirements with emoji text instead (text is the baseline every
 * model has, so it goes unmarked). */
export function modalityOptionMark(modalities?: string[] | null): string {
  if (!modalities || modalities.length === 0) return "";
  const marks = modalities
    .filter((m) => m !== "text")
    .map((m) => MODALITY_META[m]?.icon ?? m)
    .join("");
  return marks ? ` ${marks}` : "";
}
