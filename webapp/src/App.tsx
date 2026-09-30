import { NavLink, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import NetworkPage from "./pages/NetworkPage";
import BriefingsPage from "./pages/BriefingsPage";
import ManuscriptsPage from "./pages/ManuscriptsPage";
import ImagesPage from "./pages/ImagesPage";
import ChallengePage from "./pages/ChallengePage";
import { api } from "./api";

export default function App() {
  const { data: meta } = useQuery({ queryKey: ["meta"], queryFn: api.meta });

  return (
    <div className="flex flex-col h-full">
      <header className="bg-white border-b border-slate-200 px-3 md:px-6 py-2 md:py-3 flex items-center gap-2 md:gap-6 shrink-0">
        <span className="text-base md:text-xl font-bold text-slate-800 shrink-0">
          📊 <span className="hidden sm:inline">note-topix</span>
        </span>
        {meta && (
          <span className="hidden lg:inline text-xs text-slate-500 truncate">
            シグナル {meta.signal_count.toLocaleString()} 件 ·
            分類済 {meta.classifications.total.toLocaleString()} ·
            最終計算 {meta.last_run_at ?? "(未実行)"}
          </span>
        )}
        <nav className="flex gap-0.5 md:gap-1 ml-auto">
          <NavItem to="/" icon="🔍" label="Network" />
          <NavItem to="/challenge" icon="🏃" label="100日" />
          <NavItem to="/briefings" icon="📄" label="Briefings" />
          <NavItem to="/images" icon="🖼️" label="画像" />
          <NavItem to="/manuscripts" icon="📝" label="原稿" />
        </nav>
      </header>
      <main className="flex-1 overflow-hidden">
        <Routes>
          <Route path="/" element={<NetworkPage />} />
          <Route path="/challenge" element={<ChallengePage />} />
          <Route path="/briefings" element={<BriefingsPage />} />
          <Route path="/images" element={<ImagesPage />} />
          <Route path="/manuscripts" element={<ManuscriptsPage />} />
        </Routes>
      </main>
    </div>
  );
}

function NavItem({ to, icon, label }: { to: string; icon: string; label: string }) {
  return (
    <NavLink
      to={to}
      end
      className={({ isActive }) =>
        `px-2 md:px-3 py-1.5 rounded-md text-sm font-medium transition ${
          isActive
            ? "bg-blue-600 text-white"
            : "text-slate-700 hover:bg-slate-100"
        }`
      }
    >
      <span className="md:hidden">{icon}</span>
      <span className="hidden md:inline">
        {icon} {label}
      </span>
    </NavLink>
  );
}
