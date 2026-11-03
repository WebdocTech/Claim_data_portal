import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api.js";

// Loads a claims list and holds unsaved status changes until the user saves them.
export default function useClaims(loader) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState("");
  const [pending, setPendingMap] = useState(() => new Map()); // claim_id -> new status
  const [saving, setSaving] = useState(false);

  const reload = useCallback(() => {
    setError("");
    setPendingMap(new Map());
    loader()
      .then(setRows)
      .catch((e) => setError(e.message));
  }, [loader]);

  useEffect(reload, [reload]);

  // Warn before closing or reloading the browser tab with unsaved changes.
  useEffect(() => {
    if (!pending.size) return;
    const warn = (e) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [pending.size]);

  const byId = useMemo(() => new Map((rows || []).map((r) => [r.claim_id, r])), [rows]);

  // Draft `status` for the given claims; picking a claim's saved status again drops its draft.
  const setPending = useCallback(
    (claimIds, status) => {
      setPendingMap((prev) => {
        const next = new Map(prev);
        for (const id of claimIds) {
          if (byId.get(id)?.payment_status === status) next.delete(id);
          else next.set(id, status);
        }
        return next;
      });
    },
    [byId]
  );

  const discard = useCallback(() => setPendingMap(new Map()), []);

  // Save every draft (one request per target status). Returns {saved, failed: [{claim_no, error}]}.
  const save = useCallback(async () => {
    const groups = new Map();
    for (const [id, status] of pending) groups.set(status, [...(groups.get(status) || []), id]);
    setSaving(true);
    const saved = new Map();
    const failed = [];
    try {
      for (const [status, ids] of groups) {
        try {
          const { results } = await api.setStatusBulk(ids, status);
          for (const r of results) {
            if (r.ok) saved.set(r.claim_id, status);
            else failed.push({ claim_no: byId.get(r.claim_id)?.claim_no || r.claim_id, error: r.error });
          }
        } catch (e) {
          ids.forEach((id) => failed.push({ claim_no: byId.get(id)?.claim_no || id, error: e.message }));
        }
      }
      setRows((prev) =>
        prev.map((r) =>
          saved.has(r.claim_id)
            ? { ...r, payment_status: saved.get(r.claim_id), is_paid: saved.get(r.claim_id) === "Paid" }
            : r
        )
      );
      setPendingMap(new Map());
    } finally {
      setSaving(false);
    }
    return { saved: saved.size, failed };
  }, [pending, byId]);

  return { rows, error, reload, pending, setPending, discard, save, saving };
}
