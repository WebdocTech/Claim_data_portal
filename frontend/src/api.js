const TOKEN_KEY = "claims_portal_token";

export const getToken = () => sessionStorage.getItem(TOKEN_KEY);
export const setToken = (t) => (t ? sessionStorage.setItem(TOKEN_KEY, t) : sessionStorage.removeItem(TOKEN_KEY));

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function request(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`/api${path}`, { method, headers, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const detail = typeof data.detail === "string" ? data.detail : res.statusText;
    if (res.status === 401) window.dispatchEvent(new Event("portal:logout"));
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

export const api = {
  login: (username, password) => request("POST", "/auth/login", { username, password }),
  me: () => request("GET", "/me"),
  financeClaims: () => request("GET", "/finance/claims"),
  allClaims: () => request("GET", "/claims"),
  setStatus: (claimId, status) => request("POST", `/claims/${claimId}/status`, { status }),
  setStatusBulk: (claimIds, status) => request("POST", "/claims/status/bulk", { claim_ids: claimIds, status }),
  history: (claimId) => request("GET", `/claims/${claimId}/history`),
  adminSearch: (q) => request("GET", `/admin/search?q=${encodeURIComponent(q)}`),
};

export async function downloadExport(view, status) {
  const params = new URLSearchParams({ view });
  if (status) params.set("status", status);
  const res = await fetch(`/api/finance/export?${params}`, { headers: { Authorization: `Bearer ${getToken()}` } });
  if (!res.ok) throw new ApiError(res.status, "Export failed");
  const name = /filename="(.+?)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || "export.csv";
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  a.click();
  URL.revokeObjectURL(url);
}
