/** Clipboard writes are capability-checked and never reported before they resolve. */
export async function copyText(value: string): Promise<boolean> {
  const writeText = navigator.clipboard?.writeText;
  if (!writeText) return false;
  try {
    await writeText.call(navigator.clipboard, value);
    return true;
  } catch {
    return false;
  }
}
