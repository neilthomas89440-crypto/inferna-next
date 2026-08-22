import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import { useAuth } from "./context/AuthContext";
import ApiKeysPage from "./pages/ApiKeysPage";
import ClusterDetailPage from "./pages/ClusterDetailPage";
import ClustersPage from "./pages/ClustersPage";
import DashboardPage from "./pages/DashboardPage";
import InstancesPage from "./pages/InstancesPage";
import LoginPage from "./pages/LoginPage";
import ModelDetailPage from "./pages/ModelDetailPage";
import ModelsPage from "./pages/ModelsPage";
import UsersPage from "./pages/UsersPage";

export default function App() {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center text-slate-400">
        Loading…
      </div>
    );
  }

  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/clusters" element={<ClustersPage />} />
        <Route path="/clusters/:clusterId" element={<ClusterDetailPage />} />
        <Route path="/models" element={<ModelsPage />} />
        <Route path="/models/:modelId" element={<ModelDetailPage />} />
        <Route path="/instances" element={<InstancesPage />} />
        <Route path="/keys" element={<ApiKeysPage />} />
        <Route path="/users" element={<UsersPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
