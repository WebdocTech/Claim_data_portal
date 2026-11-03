import { useEffect, useState } from "react";
import { api, getToken, setToken } from "./api.js";
import Login from "./pages/Login.jsx";
import FinancePage from "./pages/FinancePage.jsx";
import ClaimsPage from "./pages/ClaimsPage.jsx";
import AdminSearch from "./pages/AdminSearch.jsx";

// Pages each role can open. Admin sees Finance and Claims read-only.
const PAGES = {
  admin: [
    ["search", "Record Search"],
    ["finance", "Finance View"],
    ["claims", "Claims View"],
  ],
  finance: [["finance", "Finance"]],
  claims: [["claims", "Claims"]],
};

export default function App() {
  const [session, setSession] = useState(null); // result of /api/me
  const [loading, setLoading] = useState(!!getToken());
  const [page, setPage] = useState(null);

  const loadSession = () =>
    api
      .me()
      .then((s) => {
        setSession(s);
        setPage(PAGES[s.user.role][0][0]);
      })
      .catch(() => setToken(null))
      .finally(() => setLoading(false));

  useEffect(() => {
    if (getToken()) loadSession();
    const onLogout = () => logout();
    window.addEventListener("portal:logout", onLogout);
    return () => window.removeEventListener("portal:logout", onLogout);
  }, []);

  function logout() {
    setToken(null);
    setSession(null);
  }

  if (loading) return <div className="center muted">Loading…</div>;
  if (!session)
    return (
      <Login
        onLogin={(token) => {
          setToken(token);
          setLoading(true);
          loadSession();
        }}
      />
    );

  const { user } = session;
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">Claims & Payments</div>
        <nav className="tabs">
          {PAGES[user.role].map(([key, label]) => (
            <button key={key} className={page === key ? "tab active" : "tab"} onClick={() => setPage(key)}>
              {label}
            </button>
          ))}
        </nav>
        <div className="who">
          <span>
            {user.full_name} <span className="role">{user.role}</span>
          </span>
          <button className="link" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>
      <main>
        {page === "search" && <AdminSearch />}
        {page === "finance" && <FinancePage session={session} />}
        {page === "claims" && <ClaimsPage session={session} />}
      </main>
    </div>
  );
}
