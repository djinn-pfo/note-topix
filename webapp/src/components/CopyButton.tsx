import { useState } from "react";

/**
 * Prominent copy-to-clipboard button. Uses navigator.clipboard.writeText where
 * available (all HTTPS / localhost / Tailscale contexts modern browsers treat
 * as secure), falls back to a hidden textarea + execCommand otherwise.
 */
export default function CopyButton({
  text,
  label = "📋 クリップボードにコピー",
}: {
  text: string;
  label?: string;
}) {
  const [state, setState] = useState<"idle" | "copied" | "error">("idle");

  async function handleCopy() {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setState("copied");
      setTimeout(() => setState("idle"), 2000);
    } catch (err) {
      console.error("clipboard copy failed:", err);
      setState("error");
      setTimeout(() => setState("idle"), 2000);
    }
  }

  const cls =
    state === "copied"
      ? "bg-green-600 hover:bg-green-700"
      : state === "error"
      ? "bg-red-600 hover:bg-red-700"
      : "bg-blue-600 hover:bg-blue-700";

  const currentLabel =
    state === "copied" ? "✅ コピー済み" : state === "error" ? "❌ 失敗" : label;

  return (
    <button
      type="button"
      onClick={handleCopy}
      className={`${cls} text-white font-semibold px-4 py-2 rounded-md shadow-sm transition text-sm`}
    >
      {currentLabel}
    </button>
  );
}
