// Unsaved-changes bar shown at the top of a page while status drafts exist.
export default function SaveBar({ pending, saving, save, discard }) {
  if (!pending.size) return null;
  const paid = [...pending.values()].filter((s) => s === "Paid").length;

  async function onSave() {
    const lines = [`Save ${pending.size} status change${pending.size === 1 ? "" : "s"}?`];
    if (paid) lines.push(`${paid} claim${paid === 1 ? "" : "s"} will be marked Paid. This cannot be undone.`);
    if (!window.confirm(lines.join("\n\n"))) return;
    const { saved, failed } = await save();
    if (failed.length) {
      const shown = failed.slice(0, 15).map((f) => `${f.claim_no}: ${f.error}`);
      if (failed.length > 15) shown.push(`…and ${failed.length - 15} more`);
      window.alert(`${saved} saved, ${failed.length} not saved:\n\n${shown.join("\n")}`);
    }
  }

  return (
    <div className="save-bar">
      <strong>
        {pending.size.toLocaleString()} unsaved change{pending.size === 1 ? "" : "s"}
      </strong>
      {paid > 0 && <span className="small-text">({paid} to Paid)</span>}
      <span className="spacer" />
      <button disabled={saving} onClick={discard}>
        Discard
      </button>
      <button className="primary" disabled={saving} onClick={onSave}>
        {saving ? "Saving…" : "Save changes"}
      </button>
    </div>
  );
}
