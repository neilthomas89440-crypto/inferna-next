/** Copy text to the clipboard; never throws. Returns "copied" | "failed". */
export async function copyText(text: string): Promise<"copied" | "failed"> {
  try {
    await navigator.clipboard.writeText(text);
    return "copied";
  } catch {
    // Clipboard API unavailable or rejected (insecure context, permission
    // denied): fall back to a hidden textarea + execCommand.
    try {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      const ok = document.execCommand("copy");
      textarea.remove();
      return ok ? "copied" : "failed";
    } catch {
      return "failed";
    }
  }
}
